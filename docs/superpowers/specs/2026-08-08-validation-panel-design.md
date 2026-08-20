# The validation panel — no work unit is done until an independent reviewer says so

*2026-08-08*

Feature order `fo-e353491c`. Planned by `wo-cd73c537`.

> **Looking for the plan rather than the reasoning?**
> [`2026-08-08-validation-panel-plan.md`](./2026-08-08-validation-panel-plan.md) is the same
> thing in rendered diagrams and almost no prose: the twelve work orders, their waves,
> both state machines, and both round sequences. This document is the *why* behind each of
> those decisions.

---

## Problem

A Jarvis work order settles itself. The worker runs `jarvis wo finish --pr <url>`, and
`ops.finish` writes `result_summary`, sets `waiting_pr_merge`, and the work order lands on
the user's merge queue. A feature order settles itself too: `Daemon.settle_features` marks
it `completed` the moment its last child completes. Nothing between a unit's own claim and
the user's inbox asks whether the work is actually finished.

That places the entire quality bar on two things: the worker's self-assessment, which is
the least independent opinion available, and the user's review, which is the attention this
OS exists to conserve. The failure it produces is recognisable — work arrives at the merge
queue with no tests, or with tests that do not exercise the change, or with a claim of
evidence the diff does not support, and the user is the first reader who notices.

**The ask.** Each working unit — **work order or feature order** — should not be claimed
done, or ready for PR review, until an *external* validator has said so: a panel of
profiled reviewers (tester, security, architect, maintainer) that reads the code changes
and the testing evidence and may reject with concrete asks. The implementor uses the
feedback and resubmits. Each round carries a fingerprint so that new evidence is provably
new. After enough rounds the loop gives up and escalates to a human. And **neither side
knows the other exists**.

---

## The three principles

Everything below follows from three rules. They are stated first because every design
decision in this document is an application of one of them.

### 1. Nobody addresses anybody directly

A work order does not talk to the project manager. The validation panel does not talk to
the work order. **Every cross-entity message is an envelope posted to a ROLE and delivered
by a router.** The sender names a role and a subject; it never names a work order id, and
it never learns who read it.

This is the user's explicit architectural constraint, and its purpose is extension: a new
participant is a new role plus a routing rule, not an edit to everything that might want to
talk to it.

### 2. The router, not the sender, decides what happens when a role is unfilled

A deferral posted by a work order with no parent feature has no project manager to reach —
and that is the overwhelmingly common case today. The sender does not branch on that. The
router files the backlog item itself, which is exactly today's behaviour. **A sender that
has to ask "does the recipient exist?" is a sender that is coupled to the recipient.**

### 3. Every judging entity sees input and produces output, and knows nothing else

The panel receives a packet and returns an outcome. The implementor receives review
feedback and produces a resubmission. The project manager receives envelopes and acts. None
of them is aware of the machinery around it, so any of them can be replaced without
touching the others.

---

## What this proposes, in one picture

```mermaid
flowchart TB
    subgraph SUB["The two submitters — the same shape, which is why one machine serves both"]
        direction LR
        IMPL["<b>THE IMPLEMENTOR</b><br/>a worker session<br/>on its own worktree"]
        PM["<b>THE PROJECT MANAGER</b><br/>a session that owns one<br/>feature order's follow-through"]
    end

    BUS{{"<b>THE MESSAGE BUS</b><br/>envelopes addressed to a ROLE, delivered by a pure router<br/>nothing below this line knows who is above it"}}

    ROUND["<b>THE ROUND MACHINE</b><br/>collects the evidence · fingerprints it · opens round N<br/>counts the rounds · decides when to give up"]

    subgraph PANEL["<b>THE VALIDATION PANEL</b>"]
        direction TB
        SEATS["tester · security · architect · maintainer"]
        ARB["arbitrate — the veto table"]
        CHAIR["chair"]
        SEATS --> ARB --> CHAIR
    end

    IMPL -- "finish --evidence" --> ROUND
    PM -- "finish --evidence" --> ROUND
    ROUND -- "packet" --> SEATS
    CHAIR -- "outcome" --> ROUND
    ROUND -- "a rejection, addressed to a role" --> BUS
    BUS -- "role: implementor" --> IMPL
    BUS -- "role: manager" --> PM

    style BUS fill:#fff3cd,stroke:#856404,stroke-width:2px
    style ROUND fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    style PANEL fill:#e2e3f3,stroke:#383d75
    style SUB fill:#e8f5e9,stroke:#2e7d32
```

The implementor and the project manager are **the same shape**: a session that submits
evidence and receives review feedback. That is what lets one round machine and one panel
serve both, and it is the whole reason the feature-order case became tractable.

---

## Scope: both units, and what makes that possible

This design was originally written for work orders only, and deferred the feature-order
case on the grounds that **a feature order has no session, so a rejection has no
addressee**. The user overruled that and supplied the missing entity: the **project manager
order**.

That resolves the objection completely, and it is worth recording why, because the two
follow-on objections dissolve with it:

| the original objection | why it no longer holds |
|---|---|
| No addressee for a rejection | The project manager order **is** the addressee. It is a session, it can be sent a message, and it can act — by filing remediation work orders under the feature. |
| The fingerprint is meaningless with no rounds | There are rounds now: the manager receives feedback, remediation lands, and the feature re-validates. The fingerprint does exactly the job it does for a work order — it proves the second submission is not the first one reworded. |
| The evidence is already validated child by child | Still true, and it is now a *feature*, not a problem. Each child passed on its own diff; what the feature-level panel adds is the **integration** question — two children individually correct and jointly wrong. That is the defect nothing else in the OS can see. |

---

## The message bus

### The envelope

```
envelopes
─────────
id            INTEGER PK
ts            REAL
project       TEXT
subject_wo_id TEXT   ──▶ work_orders(id)      the unit this is ABOUT
subject_fo_id TEXT   ──▶ feature_orders(id)   (exactly one of the two)
from_role     TEXT   reviewer | implementor | manager
to_role       TEXT   implementor | manager
kind          TEXT   review_feedback | deferral_request
payload       TEXT   JSON — the serialised form of a typed payload, never a bare dict
state         TEXT   queued | delivered | handled_by_router | undeliverable
delivered_wo_id TEXT the row it actually reached — written by the ROUTER, never
                     by the sender, and the only record of who read it
```

### The envelope is typed, and the columns are not where the typing lives

Those are `TEXT` columns, and left at that the bus would be a stringly-typed message
queue — a role misspelled at a call site becomes an envelope nobody can route, discovered
in production as an `undeliverable` row rather than at the moment the mistake was made.
The messaging substrate is the thing everything else in this feature stands on, so it gets
the strongest typing the codebase's conventions allow.

**Four layers, from the outside in:**

```python
# src/jarvis/bus.py

ROLES           = ("reviewer", "implementor", "manager")
ENVELOPE_KINDS  = ("review_feedback", "deferral_request")
ENVELOPE_STATES = ("queued", "delivered", "handled_by_router", "undeliverable")

@dataclass(frozen=True, slots=True)
class Subject:
    """Exactly one of the two is set. Constructed, never assembled from kwargs."""
    wo_id: str | None = None
    fo_id: str | None = None

@dataclass(frozen=True, slots=True)
class ReviewFeedback:          # kind="review_feedback"
    round: int
    outcome: str               # rejected | escalated
    reason: str
    asks: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class DeferralRequest:         # kind="deferral_request"
    title: str
    why: str
    neo_question_id: int | None

PAYLOADS = {"review_feedback": ReviewFeedback, "deferral_request": DeferralRequest}

def post(store, *, subject: Subject, from_role: str, to_role: str,
         payload: ReviewFeedback | DeferralRequest) -> int:
    """The ONLY writer. Derives `kind` from the payload type — a caller cannot
    disagree with itself about what it is sending. Raises BusError on an unknown
    role, a subject that is not exactly one id, or a payload of no known kind."""
```

1. **Vocabularies as module tuples**, exactly like `project_store.WO_STATUSES`. Not SQL
   `CHECK` constraints — and that is a deliberate choice, not an omission. Jarvis has **no
   `CHECK` constraint anywhere in any schema today**; every status column in the OS is
   enforced by a module tuple plus validation at the write site. More importantly, `ROLES`
   and `ENVELOPE_KINDS` are *designed to grow* — the entire justification for the bus is
   that a new participant costs a routing rule. A `CHECK` on `to_role` would make adding a
   role a schema migration, which is the cost this design exists to avoid.
2. **Frozen dataclasses for the payloads**, one per kind, so no call site ever assembles a
   dict and hopes the reader agrees about the keys. `payload` is serialised on the way in
   and parsed back into its dataclass on the way out; an envelope whose stored payload no
   longer parses goes `undeliverable` rather than being delivered as a malformed message.
3. **`kind` is derived from the payload type, never passed.** `post()` takes no `kind`
   argument. The single most likely bug in a hand-typed message bus — a `kind` that does not
   match its payload — is made unrepresentable rather than validated.
4. **A completeness test that walks the vocabularies.** For every kind in
   `ENVELOPE_KINDS`: it has an entry in `PAYLOADS`, and it has a routing rule. For every
   role in `ROLES`: `resolve` has a branch for it. Adding a kind or a role without wiring it
   fails the suite, instead of producing an `undeliverable` row at runtime six weeks later.

**The one place a `CHECK` constraint is still right** is `validation_rounds`'
`CHECK ((wo_id IS NULL) <> (fo_id IS NULL))` (see *Data model*), and the distinction is
worth stating because it is the rule for the whole feature: a `CHECK` is for a **structural
invariant that never changes and that no Python writer can be trusted to re-derive** — the
polymorphic parent must be exactly one of two, for every writer including a future repair
script. Open enumerations get module tuples. `envelopes` carries the same
exactly-one-subject `CHECK` for the same reason.

### The router

```python
# src/jarvis/bus.py — imports project_store and central_store, nothing above them
def resolve(store, envelope) -> str | None:
    """Which work order fills `to_role` for this envelope's subject? None if nobody."""

def post(store, *, subject, from_role, to_role, payload) -> int:
    """Queue an envelope. Returns its id. NEVER resolves — resolution is delivery's job.
    `kind` is derived from `type(payload)`; see *The envelope is typed*, above."""

def deliver(store, central, envelope) -> str:
    """Route one envelope. Returns the new state."""
```

Resolution rules, and they are the entire routing table:

| `to_role` | resolves to |
|---|---|
| `implementor` | the subject work order itself |
| `manager` | the `kind='manager'` work order whose `parent_id` is the subject's feature order |

**Delivery rides the existing queue.** Once resolved, `deliver` calls
`store.queue_message(wo_id, text, source=...)`, and `Daemon.deliver_messages` turns it into
the next turn on that session exactly as it does for `jarvis wo send`. No new dispatch path,
no second scheduler — the same discipline feature orders followed when they reused
`claim_next_pending` rather than inventing a queue.

**When a role is unfilled**, the router decides — principle 2:

| case | what the router does |
|---|---|
| `deferral_request`, no manager (a work order with no parent feature) | files the backlog item itself; state `handled_by_router`. This is today's behaviour, preserved exactly. |
| `review_feedback`, no implementor (the work order was cancelled or deleted) | state `undeliverable`, and the round escalates. Feedback that reached nobody must never look like feedback that was acted on. |
| the manager is cancelled but the feature is live | state `undeliverable` + attention on the feature order. A feature whose manager is gone cannot run its loop, and the user has to know. |

**Ordering and duplicates.** Envelopes are delivered oldest-first per subject, and `state`
moves to `delivered` in the same transaction as the `queue_message` insert — so a daemon
that dies between the two redelivers nothing, and a daemon that dies before either
redelivers the whole envelope. At-least-once with a transactional hand-off, which is what
the message queue already guarantees for `wo send`.

---

## The project manager order

A third `WO_KINDS` value. `WO_KINDS = ("worker", "planner", "manager")`.

```mermaid
flowchart TB
    A["jarvis fo plan fo-id --from-file plan.json"] --> B["Neo, or the user, releases the plan"]
    B --> C["<b>ProjectStore.create_plan_children</b><br/>ONE transaction"]
    C --> D["child wo 1<br/>kind=worker<br/>parent_id=fo"]
    C --> E["…"]
    C --> F["child wo N<br/>kind=worker<br/>parent_id=fo"]
    C --> G["<b>THE MANAGER ORDER</b><br/>kind=manager<br/>parent_id=fo"]

    style G fill:#fff3cd,stroke:#856404,stroke-width:2px
    style C fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
```

It is created **in the same transaction** as the children, because a feature holding
children but no manager is a feature whose deferrals and rejections have nowhere to go —
the same all-or-nothing argument that transaction already makes about the children
themselves.

### Why a new kind costs almost nothing — and the one place it does

This is the most important implementation fact in the whole document, and it was
established by reading every kind-filtered query in the codebase.

**Every kind filter in Jarvis is POSITIVE.** `kind='worker'`, never `kind != 'planner'`.
Four sites: `project_store.py:531`, `:535`, `:563` and `:734`. Because they name the kind
they want rather than the kind they exclude, a third kind is **automatically** excluded from
all of them:

| query | consequence for the manager |
|---|---|
| `feature_children` (`:734`) | the manager is not a child of the feature for settlement purposes — so it **cannot deadlock feature completion** by staying open, and cancelling it **cannot mark the feature `failed`** |
| `claim_next_pending`'s `max_parallel` clause (`:563`) | the manager is exempt from its own feature's slot cap, exactly as the planner is |
| the attention rollup (`:531`, `:535`) | the manager does not distort the "N of M children need you" line |

**The one exception, and it deadlocks a project if missed.** `count_active`
(`project_store.py:546`) has **no kind filter at all** — it counts every work order in
`ACTIVE_STATUSES`, and `waiting_input` is one of them. A manager is *designed* to sit idle in
`waiting_input` for the entire life of its feature. So:

```mermaid
flowchart LR
    A["max_concurrent: 2"] --> B["two feature orders in flight"]
    B --> C["two managers parked in<br/>waiting_input, by design"]
    C --> D{{"count_active has<br/><b>no kind filter</b>"}}
    D --> E["count_active == 2"]
    E --> F(["dispatch_pending never claims another<br/>work order. <b>The project stops.</b>"])

    style D fill:#fff3cd,stroke:#856404,stroke-width:2px
    style F fill:#f8d7da,stroke:#721c24,stroke-width:2px
```

`count_active` must exclude `kind='manager'`. A coordinator is not a piece of the work —
the same reasoning that already exempts the planner from `max_parallel`, applied to the
project-wide cap.

**Which cap is this, exactly?** `max_concurrent` is **per project, not fleet-wide**, and the
distinction changes how bad the trap is. Verified: `Daemon.dispatch_pending`
(`daemon.py:309`) loops `while store.count_active() < project.max_concurrent` against *that
project's* store; `catalog.DEFAULT_MAX_CONCURRENT` is **5**; `os.defaults.max_concurrent`
sets the fleet-wide default and each project may override it. There is **no cap on total
active work orders across the fleet** — twenty projects with `max_concurrent: 5` may run a
hundred work orders between them.

So the deadlock is not fleet-wide, and the diagram above uses `2` only because it is the
smallest number that shows it. The real shape is worse than the diagram, not better,
because it is *proportional*: at the default of 5, **every concurrent feature order in a
project permanently costs that project one of its five slots**. Three features in flight and
the project runs on two. Five and it stops. Feature orders are exactly the mechanism the OS
offers for large work, so the projects most likely to run several at once are the projects
this would strand — and it degrades silently, as a project that has simply gone quiet.

That is also why the fix needs a watchdog and not only a code change; see
*Resilience and self-healing* → `INV-MANAGER-SLOTS`.

### The other three things a long-lived idle session breaks

| where | what happens without a change |
|---|---|
| `Daemon.settle_work_order` | its default for a done turn with no `result_summary` is `needs_review` + attention "worker idle without `jarvis wo finish`". A manager is idle **by design**; without a `kind='manager'` branch sending it to `waiting_input` with no attention, every feature order carries a permanent false flag. |
| `dispatch.build_worker_prompt` (`dispatch.py:230`) | the branch is `if kind == "planner": … else: <worker contract>`. A manager falls through to the worker contract and is told to open a pull request. It needs a third branch. |
| `Daemon.settle_features` | the manager must be completed when the feature settles, or it lingers as an open work order against a closed feature. |

### What the manager is told

Its contract is short and it is not a worker's:

- You own one feature order's follow-through. You will not write product code.
- You will receive messages. Act on each one and end your turn.
- **Review feedback on the feature**: decide what has to change, file work orders under
  this feature to change it, then resubmit the feature's evidence.
- **A deferral request**: file the backlog item, recording which work order suggested it
  and which feature it came from.
- You do not know who sends you these. Do not try to find out.

---

## The two validation loops

### Work orders

```mermaid
stateDiagram-v2
    [*] --> running
    running --> validating: jarvis wo finish\n(opens round N)
    running --> needs_review: pending assumptions\n(these still outrank validation)
    needs_review --> validating: jarvis wo review --accept\n(first validation only)

    validating --> waiting_pr_merge: passed, with a pr_url
    validating --> completed: passed, no pr_url
    validating --> running: rejected\n(envelope → role implementor)
    validating --> needs_review: gave up (+ attention)
```

### Feature orders

```mermaid
stateDiagram-v2
    [*] --> planning
    planning --> plan_review
    plan_review --> executing: released — children AND the manager created
    executing --> validating: every child completed\n(opens round N)
    executing --> failed: a child failed or was cancelled

    validating --> completed: passed
    validating --> executing: rejected — the manager files\nremediation work orders
    validating --> needs_review_by_user: gave up (+ attention on the feature)
```

The two diagrams are deliberately the same shape. One round machine drives both; the only
differences are what the evidence packet contains and which role the rejection is addressed
to.

### Three properties that are load-bearing

**`validating` raises no attention**, in either machine. It is the system working. Only the
give-up transition flags anyone.

**A work order in `validating` spends a concurrency slot** (`ACTIVE_STATUSES`) because it
holds a live session the OS intends to resume. A *manager* does not, per the section above.
Those two facts look contradictory and are not: the work order will be resumed within
minutes and the manager may idle for days.

**Feature validation happens after the children have merged**, so its diff is real merged
code on the default branch. Work-order validation happens after the PR is opened and before
the merge queue, so nothing unvalidated ever reaches the user.

---

## The evidence packet

One dataclass, two collectors, distinguished by a `unit` field.

| field | `unit="work_order"` | `unit="feature"` |
|---|---|---|
| `title`, `description` | the work order's brief | the feature order's original ask |
| `summary` | the worker's `--summary` | the manager's `--summary` |
| `declared` | the worker's `--evidence` | the manager's `--evidence` |
| `base` … `head` | merge base … worktree HEAD | the feature's `base_sha` … the default branch now |
| `files`, `diff` | the worktree diff | the integrated diff of everything the children merged |
| `children` | *(absent)* | each child's title, `result_summary` and declared evidence |

**The feature's `base_sha` is recorded when the feature enters `executing`** — the default
branch's head at the moment its first child could start. Everything between that and the
default branch now IS the feature, by construction, and it needs no per-child bookkeeping.

The work-order merge-base ladder is pinned, because "which branch is the default" has no
obvious answer and would otherwise be guessed per project:

```
1. git symbolic-ref --quiet refs/remotes/origin/HEAD   → strip "refs/remotes/"
2. else origin/main   (if git rev-parse --verify succeeds)
3. else main          (if it verifies)
4. else base = ""  and  diff = git diff HEAD

With a base:  diff = git diff <base>...HEAD   PLUS   git diff HEAD
              ─────────────────────────────         ───────────────
              committed work                        anything uncommitted
```

Both halves. A worker that forgot to commit has still produced the change.

**Truncation cuts at a file boundary, never mid-hunk**, and `dropped_files` records what it
removed. **`files` is never truncated at any limit** — it is what lets a seat say *"you
claim tests were added, and no file under `tests/` appears in this diff"* even when the diff
itself was cut short.

---

## The fingerprint

```
fingerprint = sha256( full diff content BEFORE truncation
                    + whitespace-normalised `declared` text )[:16]
```

**And nothing else.** Not `head`, not `base`, not `summary`, not `pr_url`.

| a submitter that… | changes | new evidence? |
|---|---|---|
| adds an empty commit | `head` | **no** — the fingerprint must not move |
| rewords its summary | `summary` | **no** |
| re-runs the same tests, says so more confidently | `declared` whitespace | **no** |
| adds a test file | the diff | **yes** |
| states a result it had not stated before | `declared` content | **yes** |

Hashing `packet.diff` is the obvious implementation and it is wrong: the same tree would
fingerprint differently at two truncation limits, making an integrity check depend on a
display setting.

**A repeat escalates immediately and consumes no round**, compared against the
**immediately preceding** round only. A submitter that legitimately reverts to an earlier
shape because the panel told it to is not cheating.

---

## The panel

| seat | asks | veto |
|---|---|---|
| `tester` | is the change actually exercised? is the declared evidence supported by the diff? is a class of testing missing? | **yes** |
| `security` | what can this change expose, leak, or let through? | **yes** |
| `architect` | does this fit the layering, or cut across it? | no |
| `maintainer` | will the next person be able to change this? | no |
| `chair` | turns four opinions into one outcome and the prose the submitter reads | — |

### The veto table is a pure function, not a clause in the chair's prompt

```
arbitrate(opinions) -> a forced rejection, or None ("let the chair decide")

    security raises `blocking`  ──▶ REJECTED
    tester   raises `blocking`  ──▶ REJECTED
    architect,  however it replies  ──▶ nothing
    maintainer, however it replies  ──▶ nothing

    NOTHING FORCES A PASS.
    Structurally: exactly one non-None return, and its outcome is "rejected".
```

Pure — plain dicts in, a dict or None out. The input shape is deliberately a stored
`validation_opinions` row, so arbitration can be replayed over the record.

**`architect` and `maintainer` hold no veto** because their failure mode is an annoying
rejection loop, which spends exactly the time this feature exists to save. Same reasoning as
Neo's `taste` seat, and it is the negative control of the whole table: a panel where every
seat can block is a panel with no evidence the arbitration does anything.

**`blocking` is read permissively** (`bool(value)`, not `is True`), so a model writing the
string `"false"` blocks something it did not mean to. That is the only direction this can be
wrong in.

**The seats judge the packet and only the packet** — `cwd = $JARVIS_HOME`, no tools. A
headless call carries no settings file, so a tooled seat's reach would depend on the user's
global configuration rather than on anything Jarvis controls.

---

## The deferral path

The second duty the user specified, and a clean demonstration of principles 1 and 2.

```mermaid
flowchart TB
    child["a child work order,<br/>having agreed a deferral with Neo"]
    cmd["<b>jarvis wo defer</b> wo-id title --why … --neo-question id"]
    env{{"envelope<br/>kind = deferral_request<br/><b>to_role = manager</b><br/>it names a ROLE, not a manager"}}
    q{"the ROUTER asks:<br/>is the role filled?"}
    yes["delivered to the manager as a message.<br/>The manager files the backlog item."]
    no["<b>the router files it itself.</b><br/>Today's behaviour, exactly."]
    row["backlog row, carrying the relationship:<br/>origin_wo_id — who suggested it<br/>origin_fo_id — which plan it came out of<br/>origin_note — the why, and the Neo question id"]

    child --> cmd --> env --> q
    q -- "yes — the wo has a parent feature order" --> yes
    q -- "no — an ordinary standalone work order" --> no
    yes --> row
    no --> row

    style env fill:#fff3cd,stroke:#856404,stroke-width:2px
    style q fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
```

The `backlog` table today has **no relationship columns at all** (`id, project, title,
description, status, depends_on, promoted_wo_id, created_at`), so capturing "the dependency
and relationship with the plan and the work order from where it was suggested" means three
additive columns.

Note what the calling work order does **not** do: it does not check whether it has a parent
feature, it does not look up a manager, and it does not call `jarvis backlog add`. It posts
and forgets. That is principle 2 doing its job — and it is why the same command works
unchanged for a standalone work order today and for a feature child tomorrow.

---

## Where it plugs into the existing code

```mermaid
flowchart TB
    D["<b>daemon.py</b> — the ONLY module that knows about all of them<br/>_validator(cfg) · settle_work_order early return<br/>settle_features routing · deliver_envelopes"]

    OPS["<b>ops.py</b><br/>finish · review · defer"]
    BUSM["<b>bus.py</b> NEW<br/>post() · resolve() · deliver()"]
    VAL["<b>validation.py</b> NEW<br/>decide() · arbitrate()"]
    PS["<b>project_store.py</b><br/>rounds · opinions · envelopes"]
    EV["<b>evidence.py</b> NEW<br/>collect_*() · fingerprint()<br/>stdlib only"]
    SE["<b>seats.py</b> NEW<br/>Roster · run_blind"]
    NP["<b>panel.py</b><br/>Neo — behaviour unchanged"]

    D --> OPS
    D --> BUSM
    D --> VAL
    D --> PS
    OPS --> EV
    VAL --> SE
    VAL --> EV
    SE -. "extracted from, and reused by" .-> NP

    style D fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    style BUSM fill:#e8f5e9,stroke:#2e7d32
    style VAL fill:#e8f5e9,stroke:#2e7d32
    style EV fill:#e8f5e9,stroke:#2e7d32
    style SE fill:#e8f5e9,stroke:#2e7d32
```

`validation.py` must never import `neo`, `neo_store` or `panel`; `panel.py` must never
import `validation`; **and neither may import `bus`** — the round machine posts, the panel
only returns a value. The daemon is the only place any two of them are known together.

### Every touch point, and the trap it carries

| where | change | the trap |
|---|---|---|
| `count_active` (`project_store.py:546`) | exclude `kind='manager'` | **no kind filter today**; an idle manager in `waiting_input` permanently eats a project concurrency slot, and two features stop the project |
| `dispatch.py:230` | a third `kind` branch | the branch is `if planner … else worker`, so a manager silently gets the worker contract |
| `Daemon.settle_work_order` | a `kind='manager'` idle branch | its default flags an idle session `needs_review`; a manager is idle by design |
| `Daemon.settle_features` | route to `validating`; settle the manager | it looks only at `executing` features — that is what makes "flag once" true **by construction**, and routing must not break it |
| `ops.finish` | opens a round, sets `validating` | closes the backlog item only on `completed`; a new intermediate status silently stops backlog items closing |
| `ops.review_work_order` | the **second** route into done | a work order that filed assumptions goes finish → `needs_review` → review, bypassing `finish` entirely and reaching the merge queue unvalidated |
| `Daemon.settle_work_order` | early return on `validating` | it re-derives status from the latest turn on **every** tick, so without the return it sets `waiting_pr_merge` on the next one |
| `create_plan_children` | create the manager too | all-or-nothing: a feature with children and no manager has nowhere to route |
| `invariants.status_label` | a `validating` branch | it early-returns for every status that is not `pending`; a later branch is dead code |
| `invariants.true_blockers` | branches for escalated rounds | `INV-ATTENTION-REASON` rewrites any attention reason this cannot re-derive |
| `invariants.INVARIANTS` | six new `_check_*` generators registered | ids appear in timelines and `jarvis doctor` output, so renaming one rewrites history; and `check_project` runs with `repair=True` on the daemon path, so a checker that guesses repairs its guess every tick |
| `timeline.event_level` | new kinds | it returns `"signal"` for unknown kinds, so the obvious test is vacuous |
| `ui.STATUS_META` | a `validating` entry | templates index it by status; a missing key 500s the project page |
| `bootstrap.TEMPLATE_VERSION` | bumped | without it the new contract prose never reaches an already-bootstrapped project |
| `assets/validator-seats/` | **not** `assets/agents/` | `bootstrap._rebuild` copytrees `agents/` into every planner's `.claude/agents/`, so a seat dropped there becomes a bogus subagent |
| `panel.definition`'s `lru_cache` | key on the roster | `chair.md` will exist in two seat directories; a name-only key means the first one loaded poisons the other |

---

## Data model

```
validation_rounds                          validation_opinions
─────────────────                          ───────────────────
id                                         id
wo_id  ──▶ work_orders(id)     CASCADE     round_id ──▶ validation_rounds(id) CASCADE
fo_id  ──▶ feature_orders(id)  CASCADE     ts
CHECK ((wo_id IS NULL) <> (fo_id IS NULL)) seat
round        1-based, per subject          reply     the seat's raw reply, verbatim
ts                                         verdict   pass | reject | ''
fingerprint                                status    ok | abstained | failed
summary / evidence / pr_url                model / latency_ms
outcome  pending | passed | rejected        UNIQUE (round_id, seat)
         | escalated | failed
reason   what was sent back

CREATE UNIQUE INDEX … ON validation_rounds(wo_id, round) WHERE wo_id IS NOT NULL;
CREATE UNIQUE INDEX … ON validation_rounds(fo_id, round) WHERE fo_id IS NOT NULL;
```

**One polymorphic table, not two.** Both loops record identical facts, and two tables would
mean two of every reader, two renderers and two chances to disagree about what a round is.
The two nullable foreign keys keep real `ON DELETE CASCADE` for both parents, which a single
`subject_id` column could not.

**PARTIAL unique indexes, not a three-column `UNIQUE`.** SQLite treats NULLs as distinct in
a `UNIQUE` constraint, so `UNIQUE (wo_id, fo_id, round)` would enforce nothing at all on
either loop — it would look correct and silently permit duplicate rounds.

**`UNIQUE (round_id, seat)`**, not `(subject, seat)`. The nearest precedent,
`neo_store.panel_opinions`, keys on the question because a Neo question has exactly one
round. A validation has up to three, and the wrong constraint would silently drop round
two's opinions.

Additive elsewhere: `work_orders.kind` gains `manager`; `feature_orders` gains `base_sha`;
`backlog` gains `origin_wo_id`, `origin_fo_id`, `origin_note`.

---

## Configuration

`os.validation` in the catalog. **The feature ships disabled**, and at that default the OS
is byte-identical to today: same statuses, same events, same number of `claude` calls, zero
rows in any new table, and no manager order created.

| key | default | notes |
|---|---|---|
| `enabled` | **`false`** | enabling it is a separate decision, after measurement |
| `roster` | tester, security, architect, maintainer, chair | a name outside the vocabulary is a `CatalogError` |
| `seat_models` / `chair_model` | `{}` / `""` | the chair writes what a human reads |
| `timeout` | `300` | per seat, seconds |
| `max_rounds` | `3` | per unit, then escalate |
| `diff_chars` | `60000` | truncation limit |
| `feature_units` | `true` | whether feature orders validate too, independently of work orders |

**Why disabled by default, when the ask is phrased as a mandatory gate.** A round is
roughly five headless `claude` calls over a diff of up to 60 000 characters, up to three
rounds, on *every* unit in the fleet — the highest-volume path there is — and it throttles
dispatch. The precedent is Neo's panel, which shipped disabled behind an eval; the volume
here makes measuring first more important, not less.

---

## Failure modes

| situation | what happens | why |
|---|---|---|
| **empty diff** (`files == ()`) | escalate at once, **never call the panel** | a panel handed an empty diff will pass it, and that single silent failure would make the feature theatre |
| **fingerprint repeats** the previous round | escalate at once, consume no round | the submitter has stopped producing evidence |
| **`max_rounds` rejections** | escalate | the give-up the ask calls for |
| **panel unreachable** (`ClaudeCliError`) | round `failed`, retry next tick, **consume no round** | a transport outage is not a verdict |
| …3 outages in a row | escalate | not retrying for ever |
| **a seat abstains** | contributes no signal; the panel proceeds | silence is neither veto nor consent |
| **daemon restarts mid-validation** | `INV-VALIDATION-STRANDED` finds a `pending` round older than `2 × timeout`; `--repair` closes it `failed` | otherwise the unit sits in `validating` for ever with no flag |
| **an envelope reaches nobody** | `undeliverable`, and the round escalates | feedback that reached nobody must never look like feedback that was acted on |
| **the manager is cancelled, feature live** | `undeliverable` + attention on the feature | the loop cannot run and the user must know |
| **feature rejected → remediation → rejected → …** | bounded by `max_rounds`, same counter | the loop that could run for ever is the one worth bounding twice |
| **the user merges a work order's PR mid-validation** | not handled in v1 | on a pass the poller closes it within ~2 min; on a rejection the worker is told to fix merged code. Known, accepted, backlogged |
| **the user wants out** | `jarvis wo done` / `jarvis fo cancel` | they already mean "the user says this is finished" |

**Escalation goes straight to the user, not to Neo.** The ask allows "Neo or even a human".
A Neo hop needs a fourth question kind, a persona, a verdict shape and a daemon branch — and
Neo has strictly less information than the panel that just failed to settle it. Backlogged.

---

## Resilience and self-healing

The table above says what *should* happen. This section says how the OS notices when it
did not — because the two are different, and this codebase already has strong opinions
about which one is load-bearing.

`invariants.py` opens with the reason: an event log records that we set a state, not that
the state is still true ninety seconds later. Two work orders once reached the dashboard
labelled "waiting for your input" when what they needed was an assumption review; every
component behaved correctly and the resulting state was a lie. So invariants here are
**steady-state predicates re-evaluated on every reconcile tick**, never write-time
assertions, with three standing rules: no LLM ever, repair only what is unambiguous, report
once rather than every tick.

### The governing rule for this feature

> **Every new state this feature can park a unit in must have a watchdog that can un-park
> it, and the watchdog ships no later than the state does.**

This matters more here than anywhere else in the OS, because validation introduces the
first states in which a unit is *deliberately* idle and *deliberately* unflagged.
`validating` raises no attention — that is the design — which means a stuck `validating` is
indistinguishable from a working one to every human surface the OS has. The failure mode is
not an error. It is silence.

### What is newly capable of stalling

```mermaid
flowchart TB
    subgraph NEW["three new places work can stop, none of which looks like a failure"]
        direction LR
        A["a unit parked in <b>validating</b><br/>waiting on a round that will never finish"]
        B["an envelope parked in <b>queued</b><br/>waiting on a router that never ran"]
        C["a <b>manager</b> parked in waiting_input<br/>eating a project's dispatch slot"]
    end
    A --> A2(["looks like: validation in progress"])
    B --> B2(["looks like: feedback delivered"])
    C --> C2(["looks like: a quiet project"])

    style NEW fill:#f8d7da,stroke:#721c24
    style A2 fill:#fff3cd,stroke:#856404
    style B2 fill:#fff3cd,stroke:#856404
    style C2 fill:#fff3cd,stroke:#856404
```

### The watchdogs

Six invariants, in the existing registry, with stable ids because ids appear in work order
timelines and in `jarvis doctor` output. `check_project(store, repair=True)` is the daemon's
path, so "repaired" below means *automatically, every tick, and recorded*.

| id | the predicate | on violation | ships with |
|---|---|---|---|
| `INV-ENVELOPE-STUCK` | an envelope `queued` past `DELIVERY_ATTEMPT_CEILING` | **repaired**: re-attempt delivery, which is idempotent; on the last attempt mark it `undeliverable`, which `INV-VALIDATION-FEEDBACK-LOST` then surfaces | 1 `bus`, wave 1 |
| `INV-MANAGER-SLOTS` | for any project, `count_active()` ≠ the number of active **non-manager** work orders | **reported** + attention. A canary, in the mould of `INV-GATE-CANARY`: it fires the moment the `count_active` exemption regresses, rather than when the project has already gone quiet. Not repairable — a code regression is not derivable from state | 7 `manager`, wave 2 |
| `INV-VALIDATION-STRANDED` | a round `pending` for longer than `2 × timeout` | **repaired**: close the round `failed`, return the unit to a settleable state. Unambiguous — a round that outlived twice its own timeout produced no verdict, and `failed` is the one state that means exactly that | 5 `entrypoints`, wave 3 |
| `INV-VALIDATION-ORPHAN` | a unit in `validating` with no open round row at all | **reported** + attention. Deliberately *not* repaired: the correct resolution needs the status the unit came from, which is recoverable from the timeline but not from state, and rule 2 says a checker that guesses is worse than one that asks | 12 `watchdogs`, wave 3 |
| `INV-VALIDATION-FEEDBACK-LOST` | the latest round is `rejected`, but its envelope never reached anyone (`undeliverable`, or `queued` past the retry ceiling) | **reported** + attention. This is the one that would otherwise be invisible for ever: the unit is waiting for a resubmission that nobody was ever asked for | 12 `watchdogs`, wave 3 |
| `INV-MANAGER-MISSING` | a feature order in `executing` or `validating`, with children, and no `kind='manager'` child | **reported** + attention; `jarvis doctor --repair` creates the manager. Not an automatic repair, because the repair spawns a `claude` session and no watchdog in this codebase starts a process on its own | 12 `watchdogs`, wave 3 |

**Every watchdog ships in the same wave as, or before, the state it guards becomes
reachable** — that is the governing rule applied rather than quoted.

Three of them belong to one piece of machinery each and ship inside it:
`INV-ENVELOPE-STUCK` is the bus's own liveness (wave 1), `INV-MANAGER-SLOTS` is the canary
for child 7's own fix (wave 2), and `INV-VALIDATION-STRANDED` guards the `validating` state
the round machine introduces (wave 3, in `entrypoints`). Shipping a fix for a silent
project-wide stall *without* the alarm that proves the fix is still live is precisely the
gap this section exists to close.

The remaining three are cross-cutting — each joins a round to an envelope, or a feature to
its manager — and they land as a child of their own, 12 `watchdogs`, in the **same wave** as
the machinery they watch. They are deliberately **not** deferred to the end of the plan. An
earlier draft of this section did park them there, on the argument that the feature ships
disabled so nothing can stall anyway. That argument is true, and it is exactly the reasoning
that would justify deferring *any* watchdog indefinitely. A rule that yields to its own
first convenient exception is not a rule.

### What must be idempotent, and why crash-safety is a property not a patch

A daemon restart is ordinary. The validator runs off the reconcile thread and a round can
be in flight across one.

| operation | the guarantee | how |
|---|---|---|
| opening round N | **idempotent per (unit, round)** — a retried or double-delivered `finish` cannot consume two rounds | the partial unique indexes on `validation_rounds` are the enforcement, not a Python check; an `INSERT` that loses the race is caught and the existing round returned |
| recording a seat's opinion | idempotent per `(round_id, seat)` | the existing `UNIQUE (round_id, seat)` |
| delivering an envelope | at-least-once with a transactional hand-off — a daemon that dies between the `queue_message` insert and the `state` update redelivers the whole envelope, never half of it | both writes in one transaction, as already specified in *The message bus* |
| consuming a round | **a transport failure never consumes one** | rounds are counted from `outcome IN ('passed','rejected','escalated')`, never from row count, so a `failed` round is invisible to the counter |

That last row is the difference between a bounded loop and a feature that gives up on a
work order because the network was bad three times.

### The degradation ladder

```mermaid
flowchart LR
    E{"what failed?"}
    E -- "transport: the panel<br/>was unreachable" --> T["round <b>failed</b>, retry next tick<br/>consumes no round"]
    T --> T2{"3 in a row?"}
    T2 -- "no" --> T
    T2 -- "yes" --> U
    E -- "judgement: the panel<br/>rejected, max_rounds times" --> U
    E -- "integrity: the fingerprint<br/>repeats" --> U
    E -- "structural: a watchdog<br/>found a stall" --> U
    U(["<b>escalate to the user</b><br/>needs_review + attention"])
    N["<b>never</b>: a unit that is stuck<br/>and looks like it is working"]

    style U fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    style N fill:#f8d7da,stroke:#721c24,stroke-width:2px
```

Every arrow ends at the user. There is deliberately no path that ends in the OS quietly
retrying for ever, and no path that ends in a unit closing itself because validation could
not be performed — **an unavailable validator must never read as a pass.**

### Turning it off is also a failure mode

`os.validation.enabled` can go `true → false` while units are mid-round, and the naive
implementation strands every one of them in `validating` for ever: the gate that would
settle them is behind the flag that was just turned off.

> **The flag gates *opening* a round, never *settling* one.** With `enabled: false` the
> daemon still runs the validator for rounds that are already open, still delivers their
> envelopes, and still settles their units. What it stops doing is opening new ones —
> `ops.finish` reverts to today's behaviour immediately.

This makes the flag a safe stop rather than a trapdoor, which matters because it is the
only control the user has if the panel starts behaving badly at three in the morning. It is
a property of each round machine, not a watchdog, so child 4 (`loop`) owns it for work
orders and child 9 (`feature-validation`) owns it for features — each with the same test:
open a round, flip the flag, tick the daemon, assert the unit settles.

### Self-healing has a budget

Everything above is cheap SQL over the project database on a tick the daemon already runs.
No invariant here calls an LLM, and none of them is allowed to: the checker must be more
trustworthy than the thing it checks, and a validation panel is exactly the kind of thing
whose checker must not be another validation panel.

---

## Deliberately not built

| | why |
|---|---|
| a Neo triage hop before escalation | strictly less information than the panel that failed |
| enabling it by default | a catalog edit, after the eval measures cost and quality |
| roles beyond `implementor` and `manager` | the bus makes adding one cheap; adding one before anything needs it is speculative |
| per-project roster overrides | a second config merge path that buys nothing until someone has run it |
| writing validator rulings into Neo's `learnings` ledger | one shared seat namespace for two unrelated panels — see below |
| gating `pr_merge` on a validation pass | couples two review systems that escalate to different people |
| running a validator as a subagent inside the worker's session | destroys the independence that is the entire point |
| a production-corpus replay in the eval | this repository is public and the corpus would need the user to label it |

### What the seats DO read, and the correction to an earlier version of this table

An earlier draft justified keeping validator seats away from Neo's ledger by saying it
"would make a per-project validator depend on the OS-wide Neo database". **That reasoning
was wrong on its facts.** Verified against the code:

| | where it lives | scoped by |
|---|---|---|
| Neo's `learnings` (`jarvis neo learn`) | `$JARVIS_HOME/neo.db` — **one database, OS-wide**. `NeoStore()` takes no project argument. | a `project` column (`''` = everywhere) |
| the knowledge base (`jarvis learn add`) | `$JARVIS_HOME/os.db`, `knowledge` table — **also one database, OS-wide** | a `project` column (`''` = global) |

There is no per-project Neo database. Both ledgers are single OS-wide tables with a
`project` column, and workers in every project already read the second one. So "it would
introduce a cross-project dependency" describes a dependency that already exists and is
already load-bearing.

**The decision stands, for the real reason.** `learnings` carries a `seat` column whose
contract is *`''` = every seat sees it*, over the vocabulary
`neo_store.SEATS = ("premise", "record", "blast", "taste", "chair")`. The validator roster is
`tester, security, architect, maintainer, chair`. Merging them puts two unrelated panels in
one seat namespace, where **`chair` collides outright** — a ruling the user taught Neo's
chair via `jarvis neo review` would silently start steering validation verdicts. The two
ledgers are also fed by different acts: `learnings` is distilled from the user reviewing
*Neo's answers*, which says nothing about whether a diff was adequately tested.

**And there is a better substrate, already built.** The `knowledge` table is what
`jarvis learn add --project jarvis_os` writes, it is already project-scoped, it already has
retraction (`jarvis learn retract`), and worker prompts already carry an *index* of it that
the reader queries on demand. So:

> **The validator seats read the project's knowledge base, not Neo's learnings.** The seat
> prompt carries the same bounded KB index a worker gets, and a seat may cite an entry by id
> in its opinion. This is a change to child 6 (`panel`), and it is the mechanism by which
> the panel learns the user's standards over time: `jarvis learn add "this project requires
> an eval for any change to a prompt"` reaches the tester seat on the next round, with no
> new ledger, no new command and no new review flow.

---

## The decomposition

Twelve work orders. Arrows are dependency edges.

```mermaid
flowchart LR
    subgraph W1["WAVE 1 — three independent starts"]
        bus["<b>1 · bus</b>"]
        schema["<b>2 · schema</b>"]
        evidence["<b>3 · evidence</b>"]
    end
    subgraph W2["WAVE 2 — the two engines"]
        loop["<b>4 · loop</b>"]
        manager["<b>7 · manager</b>"]
    end
    subgraph W3["WAVE 3"]
        entry["<b>5 · entrypoints</b>"]
        panel["<b>6 · panel</b>"]
        defer["<b>8 · deferral</b>"]
        surf["<b>10 · surfaces</b>"]
        watch["<b>12 · watchdogs</b>"]
    end
    subgraph W4["WAVE 4"]
        fval["<b>9 · feature-validation</b>"]
    end
    subgraph W5["WAVE 5"]
        ev["<b>11 · eval</b>"]
    end

    loop --> watch
    manager --> watch
    bus --> loop
    schema --> loop
    evidence --> loop
    bus --> manager
    schema --> manager
    loop --> entry
    schema --> panel
    evidence --> panel
    loop --> panel
    manager --> defer
    schema --> surf
    loop --> surf
    manager --> surf
    manager --> fval
    panel --> fval
    loop --> fval
    panel --> ev
    fval --> ev

    style W1 fill:#e8f5e9,stroke:#2e7d32
    style W2 fill:#d1ecf1,stroke:#0c5460
    style W3 fill:#fff3cd,stroke:#856404
    style W4 fill:#f8d7da,stroke:#721c24
    style W5 fill:#e2e3f3,stroke:#383d75
```

| # | child | delivers |
|---|---|---|
| 1 | `bus` | envelopes, **the typed payloads and the vocabulary completeness test**, the pure router, delivery, the unfilled-role rule, `INV-ENVELOPE-STUCK` |
| 2 | `schema` | both `validating` statuses, `kind='manager'`, the polymorphic round tables, config, labels |
| 3 | `evidence` | the packet and the fingerprint (`unit="work_order"`) |
| 4 | `loop` | the work-order round machine; the validator is an **injected callable defaulting to `None`**; idempotent round opening |
| 5 | `entrypoints` | the `review_work_order` route, the worker contract, the `--evidence` flag's documentation, `INV-VALIDATION-STRANDED` |
| 6 | `panel` | `seats.py` extracted, `validation.py`, the five seats, **the project knowledge-base index in the seat prompt**, the daemon wiring |
| 7 | `manager` | the project manager order: creation, contract, idle settlement, **the `count_active` exemption and its `INV-MANAGER-SLOTS` canary** |
| 8 | `deferral` | `jarvis wo defer`, the envelope kind, the backlog relationship columns, the no-manager fallback |
| 9 | `feature-validation` | `FO_STATUSES` `validating`, `base_sha`, `collect_feature`, the feature round machine |
| 10 | `surfaces` | rounds, envelopes and the manager in the CLI and the dashboard |
| 11 | `eval` | a graded LLM eval of both panels, plus the free harness that keeps it honest |
| 12 | `watchdogs` | the three cross-cutting invariants: `INV-VALIDATION-ORPHAN`, `INV-VALIDATION-FEEDBACK-LOST`, `INV-MANAGER-MISSING` |

**Why twelve and not eight.** This is now four things: a messaging substrate, a new
long-lived entity, two validation loops that share one panel, and the watchdogs that keep
all of it from stalling silently. The child cap exists to bound the blast radius of a plan
nobody reads closely — the honest response to exceeding it is to say so and let the user
decide, not to make six children into three big ones.

**`loop` must not be split further.** `finish → validating`, the off-thread runner and the
reconciler early return each leave a state where a finished work order is lost if shipped
without the others. Everything that can safely land after that seam is in `entrypoints`.

**Why `watchdogs` is a child and not a section of another one.** Its three invariants are
the cross-cutting ones: each joins a round to an envelope, or a feature to its manager, so
each needs `loop` *and* `bus` *and* `manager` to exist. Folding them into `entrypoints`
would give that child three unrelated gaps plus four invariants — the shape this plan was
already sent back for once. It sits in wave 3 with the rest, not at the end: a watchdog that
ships after the state it guards is a watchdog that was not there for the first failure.

---

## What this design does not know yet

1. **Do the seats reject the right things?** Only a graded eval can say. The design commits
   to the *structure* — nothing forces a pass, two seats hold vetoes — and leaves the
   judgement quality to measurement.
2. **How often does a submitter actually pass `--evidence`?** The flag is optional, because
   requiring it would break every worker in flight.
3. **Is the integrated feature diff a reviewable object?** A feature of six children may be
   a very large diff. Truncation makes it safe but perhaps not useful, and the feature-level
   panel may need a different roster or a summarising pass. The eval is where that shows up.
4. **What does a round cost?** Unmeasured. The eval prints the number and asserts nothing
   about it: there is no baseline, and a test that failed on cost would spend real money to
   be flaky.
