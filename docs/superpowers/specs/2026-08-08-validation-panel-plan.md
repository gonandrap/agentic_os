# The plan, in diagrams

*Feature order `fo-e353491c` — "no work unit is done until an independent validator says so"*

This is the **plan**: what gets built, in what order, by how many work orders, and how the
pieces fit. Every section is a picture with one line of caption.

- The reasoning behind each decision lives in the design document:
  [`2026-08-08-validation-panel-design.md`](./2026-08-08-validation-panel-design.md)
- Absolute path on this machine:
  `/home/gonzalo/workspace/agentic_os/docs/superpowers/specs/2026-08-08-validation-panel-plan.md`

---

## 1. What is being built

Two submitters, one bus, one round machine, one panel. Nothing on the left knows anything
on the right exists.

```mermaid
flowchart TB
    subgraph SUB["The two submitters — the same shape, which is why one machine serves both"]
        direction LR
        IMPL["<b>THE IMPLEMENTOR</b><br/>a worker session<br/>on its own worktree"]
        PM["<b>THE PROJECT MANAGER</b><br/>a session that owns one<br/>feature order's follow-through"]
    end

    BUS{{"<b>THE MESSAGE BUS</b><br/>envelopes addressed to a ROLE, delivered by a pure router<br/>the sender never names a work order, and never learns who read it"}}

    ROUND["<b>THE ROUND MACHINE</b><br/>collects the evidence · fingerprints it · opens round N<br/>counts the rounds · decides when to give up"]

    subgraph PANEL["<b>THE VALIDATION PANEL</b> — sees the packet, and only the packet"]
        direction TB
        SEATS["tester · security · architect · maintainer<br/>one blind concurrent round"]
        ARB["arbitrate — a pure veto table<br/>nothing forces a pass"]
        CHAIR["chair — one outcome, plus the prose the submitter reads"]
        SEATS --> ARB --> CHAIR
    end

    IMPL -- "jarvis wo finish --evidence" --> ROUND
    PM -- "resubmits the feature's evidence" --> ROUND
    ROUND -- "the packet" --> SEATS
    CHAIR -- "the outcome" --> ROUND
    ROUND -- "a rejection, addressed to a role" --> BUS
    BUS -- "role: implementor" --> IMPL
    BUS -- "role: manager" --> PM

    style BUS fill:#fff3cd,stroke:#856404,stroke-width:2px
    style ROUND fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    style PANEL fill:#e2e3f3,stroke:#383d75
    style SUB fill:#e8f5e9,stroke:#2e7d32
```

### What travels on the bus is typed

The columns are `TEXT`, but no caller ever sees a string or a bare dict. The typing is at
the boundary, which is where a wrong role or a mismatched payload can still be caught:

```mermaid
flowchart LR
    CALL["a caller"] --> P["<b>post()</b> — the only writer<br/>takes a typed payload<br/><b>derives kind from its type</b>"]
    P --> V{"validates"}
    V -- "role not in ROLES" --> X(["BusError, at the call site"])
    V -- "subject not exactly one id" --> X
    V -- "payload of no known kind" --> X
    V -- "ok" --> ROW["envelope row<br/>payload serialised"]
    ROW --> RD["<b>deliver()</b> parses the payload<br/>back into its dataclass"]
    RD --> BAD(["unparseable → undeliverable,<br/>never delivered as a malformed message"])

    style P fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    style X fill:#f8d7da,stroke:#721c24
    style BAD fill:#fff3cd,stroke:#856404
```

`ROLES`, `ENVELOPE_KINDS` and `ENVELOPE_STATES` are module tuples — the same pattern as
`project_store.WO_STATUSES`, and deliberately **not** SQL `CHECK` constraints: Jarvis has
none anywhere today, and roles are *designed to grow*, so a `CHECK` on `to_role` would make
adding a role a schema migration. A completeness test walks both vocabularies and fails if a
kind has no payload type or a role has no routing branch, so the gap shows up in the suite
rather than as an `undeliverable` row months later.

---

## 2. The plan itself — 12 work orders, 5 waves

Boxes are work orders. Arrows are dependency edges (`--depends-on`). A wave is everything
that can run at the same time.

```mermaid
flowchart LR
    subgraph W1["WAVE 1 — foundations, no dependencies, all three run at once"]
        direction TB
        bus["<b>1 · bus</b><br/>typed, role-addressed envelopes<br/>the pure router · INV-ENVELOPE-STUCK"]
        schema["<b>2 · schema</b><br/>both validating statuses<br/>kind=manager · round tables · config"]
        evidence["<b>3 · evidence</b><br/>the evidence packet<br/>+ the round fingerprint"]
    end

    subgraph W2["WAVE 2 — the two engines"]
        direction TB
        loop["<b>4 · loop</b><br/>the work-order round machine<br/>finish opens a round; the daemon runs it"]
        manager["<b>7 · manager</b><br/>the project manager order<br/>count_active exemption · INV-MANAGER-SLOTS"]
    end

    subgraph W3["WAVE 3 — everything that hangs off the engines"]
        direction TB
        entry["<b>5 · entrypoints</b><br/>the review route into done<br/>worker contract · stranded-round invariant"]
        panel["<b>6 · panel</b><br/>the validator seats<br/>arbitrate · validation.py"]
        defer["<b>8 · deferral</b><br/>jarvis wo defer<br/>+ backlog relationship columns"]
        surf["<b>10 · surfaces</b><br/>rounds, envelopes and the manager<br/>in the CLI and the dashboard"]
        watch["<b>12 · watchdogs</b><br/>the three cross-cutting invariants<br/>that catch a silent stall"]
    end

    subgraph W4["WAVE 4 — the feature half"]
        fval["<b>9 · feature-validation</b><br/>the feature round machine<br/>the integrated diff · rejection to the manager"]
    end

    subgraph W5["WAVE 5 — measurement, then the decision to enable"]
        ev["<b>11 · eval</b><br/>a graded LLM eval of BOTH panels"]
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

**The whole feature ships disabled.** `os.validation.enabled = false` until child 11 has
measured what a round costs and whether the seats reject the right things. Every child brief
carries the same sentence: *with `os.validation.enabled` false, this path must not run and
the OS behaves exactly as it does today.*

---

## 3. The 12 work orders

| # | key | title | depends on |
|---|---|---|---|
| 1 | `bus` | Build the message bus: typed, role-addressed envelopes and a pure router | — |
| 2 | `schema` | Add both `validating` statuses, the `manager` work-order kind and the polymorphic validation tables | — |
| 3 | `evidence` | Build the evidence packet and the round fingerprint | — |
| 4 | `loop` | Build the work-order round machine: `finish` opens a round, the daemon runs the validator off-thread, the reconciler stands back | bus, schema, evidence |
| 5 | `entrypoints` | Close the assumption-review route into done, tell workers about `--evidence`, and catch a stranded round | loop |
| 6 | `panel` | Ship the validator seats, the arbitration table and the validation panel | schema, evidence, loop |
| 7 | `manager` | Create the project manager order and fix everything a long-lived idle work order breaks | bus, schema |
| 8 | `deferral` | Add `jarvis wo defer`: route deferred work to the project manager, or to the backlog when there is none | manager |
| 9 | `feature-validation` | Validate feature orders: the integrated diff, the feature round machine, and rejections routed to the manager | manager, panel, loop |
| 10 | `surfaces` | Render validation rounds, envelopes and the manager order in the CLI and the dashboard | schema, loop, manager |
| 11 | `eval` | Grade both validation panels with a synthetic LLM eval and prove the eval is wired | panel, feature-validation |
| 12 | `watchdogs` | Catch the three ways a validation can stall while still looking like it is working | loop, manager |

---

## 4. What happens to a work order

`validating` is the one new state. It raises **no attention** — it is the system working.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> running
    running --> validating: jarvis wo finish — opens round N
    running --> needs_review: pending assumptions still outrank validation
    needs_review --> validating: jarvis wo review --accept

    validating --> waiting_pr_merge: PASSED, with a pr_url
    validating --> completed: PASSED, no pr_url
    validating --> running: REJECTED — envelope to role implementor
    validating --> needs_review: GAVE UP — the only transition that flags the user

    waiting_pr_merge --> [*]
    completed --> [*]
```

## 5. What happens to a feature order

Deliberately the same shape. One round machine drives both.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> planning
    planning --> plan_review
    plan_review --> executing: released — children AND the manager created
    executing --> validating: every child completed — opens round N
    executing --> failed: a child failed or was cancelled

    validating --> completed: PASSED
    validating --> executing: REJECTED — the manager files remediation work orders
    validating --> needs_review: GAVE UP — attention on the feature
    completed --> [*]
```

---

## 6. One validation round, end to end

```mermaid
sequenceDiagram
    autonumber
    participant W as Implementor
    participant O as ops.finish
    participant D as daemon
    participant V as validation.decide
    participant P as the four seats
    participant B as the message bus

    W->>O: jarvis wo finish --pr URL --evidence ...
    O->>O: collect the packet, fingerprint it
    O->>O: open round N, set status = validating
    Note over W,O: the session is parked. No attention is raised.
    D->>V: next tick, off the reconcile thread
    V->>P: the packet, and only the packet. No tools.
    P-->>V: four independent opinions
    V->>V: arbitrate — does security or tester raise blocking?

    alt PASSED
        V-->>D: passed
        D->>D: waiting_pr_merge, or completed when there is no PR
    else REJECTED
        V-->>B: envelope, to_role = implementor
        B->>W: delivered as a queued message, and the worker resumes
        W->>O: new evidence — round N+1
    else GAVE UP
        Note over V: round cap reached, OR the fingerprint<br/>repeats the preceding round
        V-->>D: escalated
        D->>D: needs_review + attention — straight to the user
    end
```

**The fingerprint** is what stops a resubmission that changes nothing. It is computed over
the declared evidence and the tree, not over the truncated diff text — hashing the displayed
diff would make an integrity check depend on a display setting. A repeat escalates
immediately and **consumes no round**.

## 7. One feature round, end to end

The only difference: the packet is an integrated diff of merged code, and the rejection is
addressed to `role: manager` instead of `role: implementor`.

```mermaid
sequenceDiagram
    autonumber
    participant D as daemon settle_features
    participant V as validation.decide
    participant B as the message bus
    participant M as Project manager order
    participant C as remediation work orders

    D->>D: last child completed, feature status = validating
    D->>V: the integrated diff since base_sha, plus every child's evidence
    V-->>B: REJECTED — envelope, to_role = manager
    B->>M: delivered as a queued message
    Note over M: the manager does not know who sent it,<br/>and does not try to find out
    M->>C: files work orders under this feature
    C-->>D: they complete
    D->>D: feature back to validating — round N+1
```

---

## 8. Where deferred work goes

The clean demonstration of the decoupling rule: **the router, not the sender, decides what
happens when a role is unfilled.**

```mermaid
flowchart TB
    child["a child work order,<br/>having agreed a deferral with Neo"]
    cmd["<b>jarvis wo defer</b> wo-id title --why ... --neo-question id"]
    env{{"envelope<br/>kind = deferral_request<br/><b>to_role = manager</b>"}}
    q{"the ROUTER asks:<br/>is the role filled?"}
    yes["delivered to the manager as a message.<br/>The manager files the backlog item."]
    no["<b>the router files it itself.</b><br/>Exactly today's behaviour."]
    row["backlog row, now carrying the relationship:<br/>origin_wo_id · origin_fo_id · origin_note"]

    child --> cmd --> env --> q
    q -- "yes — the wo has a parent feature" --> yes
    q -- "no — an ordinary standalone work order" --> no
    yes --> row
    no --> row

    style env fill:#fff3cd,stroke:#856404,stroke-width:2px
    style q fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
```

What the calling work order does **not** do: check whether it has a parent feature, look up
a manager, or call `jarvis backlog add`. It posts and forgets. That is why the same command
works unchanged for a standalone work order today and a feature child tomorrow.

---

## 9. Who can block, and who cannot

```mermaid
flowchart LR
    T["<b>tester</b><br/>is the change actually exercised?<br/>is a class of testing missing?"]
    S["<b>security</b><br/>what can this expose, leak,<br/>or let through?"]
    A["<b>architect</b><br/>does this fit the layering,<br/>or cut across it?"]
    M["<b>maintainer</b><br/>will the next person<br/>be able to change this?"]
    R(["REJECTED"])
    C["<b>chair</b> decides"]

    T -- "blocking — VETO" --> R
    S -- "blocking — VETO" --> R
    A -- "advisory only" --> C
    M -- "advisory only" --> C

    style T fill:#f8d7da,stroke:#721c24
    style S fill:#f8d7da,stroke:#721c24
    style A fill:#e8f5e9,stroke:#2e7d32
    style M fill:#e8f5e9,stroke:#2e7d32
```

`architect` and `maintainer` hold no veto because their failure mode is an annoying
rejection loop, which spends exactly the time this feature exists to save. **Nothing forces
a pass** — `arbitrate` has exactly one non-`None` return, and its outcome is `rejected`.

---

## 10. What keeps it from stalling silently

`validating` raises no attention — that is the design. Which means a *stuck* `validating`
looks exactly like a working one. Three new places work can stop, none of which reads as a
failure:

```mermaid
flowchart TB
    subgraph NEW["the new silent stalls"]
        direction LR
        A["a unit parked in <b>validating</b><br/>on a round that will never finish"]
        B["an envelope parked in <b>queued</b><br/>that the router never picked up"]
        C["a <b>manager</b> in waiting_input<br/>eating a dispatch slot"]
    end
    A --> A2(["looks like:<br/>validation in progress"])
    B --> B2(["looks like:<br/>feedback delivered"])
    C --> C2(["looks like:<br/>a quiet project"])

    style NEW fill:#f8d7da,stroke:#721c24
    style A2 fill:#fff3cd,stroke:#856404
    style B2 fill:#fff3cd,stroke:#856404
    style C2 fill:#fff3cd,stroke:#856404
```

So every new parked state gets a watchdog in `invariants.py` — steady-state predicates
re-checked on every reconcile tick, no LLM, repairing only what is unambiguous:

| id | fires when | does | ships in |
|---|---|---|---|
| `INV-ENVELOPE-STUCK` | an envelope is `queued` past the retry ceiling | **repairs** — retries, then marks `undeliverable` | 1 `bus`, wave 1 |
| `INV-MANAGER-SLOTS` | `count_active()` ≠ active non-manager work orders | reports + attention — the canary for §12 | 7 `manager`, wave 2 |
| `INV-VALIDATION-STRANDED` | a round is `pending` past `2 × timeout` | **repairs** — closes it `failed`, unit settles | 5 `entrypoints`, wave 3 |
| `INV-VALIDATION-ORPHAN` | a unit is `validating` with no round row | reports + attention | 12 `watchdogs`, wave 3 |
| `INV-VALIDATION-FEEDBACK-LOST` | a rejection's envelope reached nobody | reports + attention | 12 `watchdogs`, wave 3 |
| `INV-MANAGER-MISSING` | a live feature has children but no manager | reports; `jarvis doctor --repair` creates it | 12 `watchdogs`, wave 3 |

Everything that can go wrong ends at the same place:

```mermaid
flowchart LR
    E{"what failed?"}
    E -- "transport: panel unreachable" --> T["round <b>failed</b>, retry next tick<br/>consumes no round"]
    T --> T2{"3 in a row?"}
    T2 -- "no" --> T
    T2 -- "yes" --> U
    E -- "judgement: rejected max_rounds times" --> U
    E -- "integrity: the fingerprint repeats" --> U
    E -- "structural: a watchdog found a stall" --> U
    U(["<b>escalate to the user</b><br/>needs_review + attention"])
    N["<b>no path ends here:</b><br/>stuck, and looking like it works"]

    style U fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    style N fill:#f8d7da,stroke:#721c24,stroke-width:2px
```

**An unavailable validator must never read as a pass**, and **turning the feature off must
not strand what is already running**: `os.validation.enabled` gates *opening* a round, never
*settling* one, so flipping it to `false` mid-flight lets every open round finish and simply
stops new ones. That makes the kill switch a safe stop instead of a trapdoor.

---

## 11. Where it lands in the existing code

```mermaid
flowchart TB
    D["<b>daemon.py</b> — the ONLY module that knows about all of them<br/>_validator · settle_work_order early return · settle_features routing · deliver_envelopes"]

    OPS["<b>ops.py</b><br/>finish · review · defer"]
    BUSM["<b>bus.py</b> NEW<br/>post · resolve · deliver<br/>typed payloads · ROLES · KINDS"]
    VAL["<b>validation.py</b> NEW<br/>decide · arbitrate"]
    PS["<b>project_store.py</b><br/>rounds · opinions · envelopes"]
    EV["<b>evidence.py</b> NEW<br/>collect · fingerprint<br/>stdlib only"]
    SE["<b>seats.py</b> NEW<br/>Roster · run_blind"]
    NP["<b>panel.py</b><br/>Neo — behaviour unchanged"]

    D --> OPS
    D --> BUSM
    D --> VAL
    D --> PS
    OPS --> EV
    VAL --> SE
    VAL --> EV
    SE -.-> NP

    style D fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    style BUSM fill:#e8f5e9,stroke:#2e7d32
    style VAL fill:#e8f5e9,stroke:#2e7d32
    style EV fill:#e8f5e9,stroke:#2e7d32
    style SE fill:#e8f5e9,stroke:#2e7d32
```

`validation.py` must never import `neo`, `neo_store` or `panel`; `panel.py` must never
import `validation`; and **neither may import `bus`**. The daemon is the only place any two
of them are known together — that is the decoupling, expressed as an import rule a test can
check.

---

## 12. The one thing that stops a project if it is missed

```mermaid
flowchart LR
    A["max_concurrent — <b>per project</b>,<br/>default 5"] --> B["N feature orders in flight<br/>in that project"]
    B --> C["N managers parked<br/>in waiting_input, by design"]
    C --> D{{"count_active has<br/><b>no kind filter</b><br/>project_store.py:546"}}
    D --> E["N of the project's slots<br/>are permanently spent"]
    E --> F(["at N = max_concurrent,<br/>dispatch_pending claims nothing.<br/><b>The project stops.</b>"])

    style D fill:#fff3cd,stroke:#856404,stroke-width:2px
    style F fill:#f8d7da,stroke:#721c24,stroke-width:2px
```

**The cap is per project, not fleet-wide.** `Daemon.dispatch_pending` loops
`while store.count_active() < project.max_concurrent` against that one project's store;
`os.defaults.max_concurrent` only sets the default, which each project may override; nothing
caps the fleet as a whole. So the damage is proportional rather than absolute — **every
concurrent feature order in a project permanently costs that project one of its five
slots.** Three features and it runs on two; five and it stops. Feature orders are the
mechanism the OS offers for large work, so the projects most likely to run several at once
are exactly the ones this strands.

Child 7 (`manager`) carries the fix — `count_active` must exclude `kind='manager'` — and,
because a silent stall deserves an alarm and not just a patch, the `INV-MANAGER-SLOTS`
canary that fires if the exemption ever regresses. Every *other* kind filter in Jarvis is
positive (`kind='worker'`, never `kind != …`), so a third kind is automatically excluded
from all of them. This is the single exception.

---

## 13. Why twelve and not eight

The cap is eight. This plan asks for twelve, and says so rather than hiding it in four
oversized children.

```mermaid
flowchart LR
    ask["the ask"] --> a["a messaging substrate<br/><i>bus</i>"]
    ask --> b["a new long-lived entity<br/><i>manager, deferral</i>"]
    ask --> c["two validation loops<br/>sharing one panel<br/><i>loop, panel, feature-validation</i>"]
    ask --> d["watchdogs for every new<br/>state that can stall<br/><i>watchdogs</i>"]
    a --> out(["12 children, each<br/>one session's work"])
    b --> out
    c --> out
    d --> out
```

The child cap exists to bound the blast radius of a plan nobody reads closely. The honest
response to exceeding it is to say so and let the user decide — not to fold six children
into three big ones. **`loop` in particular must not be split further**: `finish →
validating`, the off-thread runner and the reconciler early return each leave a state where
a finished work order is *lost* if shipped alone. Everything that can safely land after that
seam is already split out into `entrypoints`.

**Every watchdog ships in the wave that creates the state it guards** — three inside the
child that owns that machinery, three as `watchdogs` because they are cross-cutting (each
joins a round to an envelope, or a feature to its manager). None is deferred to the end. A
watchdog that arrives after the state it guards is a watchdog that was not there for the
first failure.

---

## 14. What this plan does not know yet

| open question | where it gets answered |
|---|---|
| Do the seats reject the right things? | child 11, the graded eval |
| What does a round cost? | child 11 prints it and asserts nothing — there is no baseline |
| Is a whole feature's diff a reviewable object? | child 11; may need a different roster or a summarising pass |
| How often will a worker actually pass `--evidence`? | measured after enabling; the flag is optional so nothing in flight breaks |

Enabling the feature is a **separate decision, after the eval** — one catalog edit.
