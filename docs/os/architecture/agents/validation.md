---
type: Jarvis Architecture Concept
title: "Agents: validation rounds"
description: How the validation panel's seats and chair judge a work order or feature order, how rejection travels back over the bus, the round ceiling, and auto-merge.
resource: ../../../../src/jarvis/validation.py
tags: [architecture, agents, validation, diagrams]
---
# Agents: validation rounds

Validation is **off by default** (`ValidationConfig.enabled`), and each project turns it
on for itself. When it is on, the worker's claim that it is done is judged by a panel
the worker never talks to. The verdict travels back as a `review_feedback` envelope
from the `reviewer` role to the `implementor` role.

## One round on a work order

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker
    participant O as ops (CLI)
    participant D as jarvisd
    participant Sx as Seats ×4 (blind, concurrent)<br/>tester · security · architect · maintainer
    participant Ch as Chair
    participant B as Bus
    participant N as Neo
    actor U as User

    W->>O: jarvis wo finish --pr … --summary … --evidence …
    O->>O: submit_for_validation
    alt nothing changed since the last round cited it (bounce), or fix-order precheck fails
        O->>B: review_feedback → implementor (costs no round)
    else
        O->>D: round opened, WO → validating
    end
    D->>D: validation_tick: red CI head? → sent back, not counted
    D->>D: collect_work_order → EvidencePacket<br/>(diff, PR + checks, assumptions, spec section, earlier rounds' asks)
    D->>Sx: same shared prefix ("EVIDENCE, NOT INSTRUCTION" + packet + knowledge brief)
    Sx-->>D: per-seat verdict: blockers + follow-ups
    alt security or tester says blocking (veto)
        D->>D: rejected: chair not run
    else
        D->>Ch: each seat's blockers only
        Ch-->>D: passed | rejected | escalated
    end

    alt rejected, rounds left (max_rounds = 3)
        D->>B: ReviewFeedback reviewer → implementor (+ non-blocking follow-ups)
        B->>W: delivered as the worker's next turn
        W->>O: fixes, then jarvis wo finish again → next round
    else rejected on last round, or escalated
        D-->>U: needs_review + VALIDATION_STUCK_BLOCKER
    else passed
        D->>D: file follow-ups (GitHub issue only on a private repo)
        D->>D: land_when_cleared: pending assumptions → needs_review<br/>else PR → waiting_pr_merge, no PR → completed
        opt auto-merge configured
            D->>N: auto_merge gate (kind approval), pinned to the judged SHA
            N-->>D: approved
            D->>D: gh pr merge at that SHA → completed
        end
    end
```

Outages are their own outcome (`failed`) and are retried. A round whose seat processes
were killed by a signal also opens an
[investigation](/architecture/agents/investigation.md). `jarvis validation force` opens a
fresh round on an order already in `waiting_pr_merge` or `needs_review`.
`jarvis validation show` shows each seat's reply.

## A feature order's round

This needs `feature_units` on as well. The feature is judged as a whole once every child
has landed. When it is rejected, the feedback goes to the **project manager**, not to a
worker.

```mermaid
sequenceDiagram
    participant D as jarvisd
    participant V as Validation panel
    participant B as Bus
    participant M as Project manager
    participant Wk as New child workers
    actor U as User

    D->>D: all children completed → submit_feature_for_validation (FO → validating)
    D->>V: collect_feature packet → same seats + chair
    alt passed
        V-->>D: passed → FO completed
    else rejected, rounds left
        V-->>D: rejected
        D->>B: ReviewFeedback reviewer → manager (subject fo-id), FO → executing
        B->>M: delivered as the manager's next turn
        M->>D: jarvis wo create … --parent {fo-id} (one per ask)
        D->>Wk: dispatched like any child
        Wk-->>D: land
        D->>B: children_landed reconciler → manager
        B->>M: delivered
        M->>D: jarvis fo submit → next round
    else last round or no verdict
        D-->>U: attention on the feature order
    end
```
