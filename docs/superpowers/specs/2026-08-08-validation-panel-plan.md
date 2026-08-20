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

---

## 2. The plan itself — 11 work orders, 5 waves

Boxes are work orders. Arrows are dependency edges (`--depends-on`). A wave is everything
that can run at the same time.

```mermaid
flowchart LR
    subgraph W1["WAVE 1 — foundations, no dependencies, all three run at once"]
        direction TB
        bus["<b>1 · bus</b><br/>role-addressed envelopes<br/>+ the pure router"]
        schema["<b>2 · schema</b><br/>both validating statuses<br/>kind=manager · round tables · config"]
        evidence["<b>3 · evidence</b><br/>the evidence packet<br/>+ the round fingerprint"]
    end

    subgraph W2["WAVE 2 — the two engines"]
        direction TB
        loop["<b>4 · loop</b><br/>the work-order round machine<br/>finish opens a round; the daemon runs it"]
        manager["<b>7 · manager</b><br/>the project manager order<br/>+ the count_active exemption"]
    end

    subgraph W3["WAVE 3 — everything that hangs off the engines"]
        direction TB
        entry["<b>5 · entrypoints</b><br/>the review route into done<br/>worker contract · stranded-round invariant"]
        panel["<b>6 · panel</b><br/>the validator seats<br/>arbitrate · validation.py"]
        defer["<b>8 · deferral</b><br/>jarvis wo defer<br/>+ backlog relationship columns"]
        surf["<b>10 · surfaces</b><br/>rounds, envelopes and the manager<br/>in the CLI and the dashboard"]
    end

    subgraph W4["WAVE 4 — the feature half"]
        fval["<b>9 · feature-validation</b><br/>the feature round machine<br/>the integrated diff · rejection to the manager"]
    end

    subgraph W5["WAVE 5 — measurement, then the decision to enable"]
        ev["<b>11 · eval</b><br/>a graded LLM eval of BOTH panels"]
    end

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

## 3. The 11 work orders

| # | key | title | depends on |
|---|---|---|---|
| 1 | `bus` | Build the message bus: role-addressed envelopes and a pure router | — |
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

## 10. Where it lands in the existing code

```mermaid
flowchart TB
    D["<b>daemon.py</b> — the ONLY module that knows about all of them<br/>_validator · settle_work_order early return · settle_features routing · deliver_envelopes"]

    OPS["<b>ops.py</b><br/>finish · review · defer"]
    BUSM["<b>bus.py</b> NEW<br/>post · resolve · deliver"]
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

## 11. The one thing that stops the fleet if it is missed

```mermaid
flowchart LR
    A["max_concurrent: 2"] --> B["two feature orders in flight"]
    B --> C["two managers parked<br/>in waiting_input, by design"]
    C --> D{{"count_active has<br/><b>no kind filter</b><br/>project_store.py:546"}}
    D --> E["count_active == 2"]
    E --> F(["dispatch_pending never claims<br/>another work order.<br/><b>The project stops.</b>"])

    style D fill:#fff3cd,stroke:#856404,stroke-width:2px
    style F fill:#f8d7da,stroke:#721c24,stroke-width:2px
```

Child 7 (`manager`) carries the fix: `count_active` must exclude `kind='manager'`. Every
*other* kind filter in Jarvis is positive (`kind='worker'`, never `kind != …`), so a third
kind is automatically excluded from all of them — this is the single exception, and it is a
project-wide deadlock.

---

## 12. Why eleven and not eight

The cap is eight. This plan asks for eleven, and says so rather than hiding it in three
oversized children.

```mermaid
flowchart LR
    ask["the ask"] --> a["a messaging substrate<br/><i>bus</i>"]
    ask --> b["a new long-lived entity<br/><i>manager, deferral</i>"]
    ask --> c["two validation loops<br/>sharing one panel<br/><i>loop, panel, feature-validation</i>"]
    a --> out(["11 children, each<br/>one session's work"])
    b --> out
    c --> out
```

The child cap exists to bound the blast radius of a plan nobody reads closely. The honest
response to exceeding it is to say so and let the user decide — not to fold five children
into three big ones. **`loop` in particular must not be split further**: `finish →
validating`, the off-thread runner and the reconciler early return each leave a state where
a finished work order is *lost* if shipped alone. Everything that can safely land after that
seam is already split out into `entrypoints`.

---

## 13. What this plan does not know yet

| open question | where it gets answered |
|---|---|
| Do the seats reject the right things? | child 11, the graded eval |
| What does a round cost? | child 11 prints it and asserts nothing — there is no baseline |
| Is a whole feature's diff a reviewable object? | child 11; may need a different roster or a summarising pass |
| How often will a worker actually pass `--evidence`? | measured after enabling; the flag is optional so nothing in flight breaks |

Enabling the feature is a **separate decision, after the eval** — one catalog edit.
