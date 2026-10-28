---
type: Jarvis Architecture Concept
title: "Agents: work order"
description: How the user, Jarvis, the daemon, a worker and its crew, and Neo interact over one work order, from creation to settlement.
tags: [architecture, agents, work orders, diagrams]
---
# Agents: work order

Roster and conventions: [Agents of the OS](/architecture/agents/overview.md). The full
state machine and transport are in
[Work-order lifecycle](/architecture/work-order-lifecycle.md).

## From ask to settlement

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant J as Jarvis (operator)
    participant D as jarvisd
    participant W as Worker (lead)
    participant C as Crew subagent<br/>(implementer / spec-writer)
    participant N as Neo
    participant GH as GitHub

    U->>J: "fix the login bug in project X"
    J->>D: jarvis wo create X "…" -d "{full intent}"  (status pending)
    Note over D: dispatch_pending: claim_next_pending<br/>under max_concurrent, depends_on, budget
    D->>W: turn 1: claude -p --session-id … --worktree {wo-id}<br/>(briefing + knowledge index + one OKF playbook)
    W->>C: Task (background) with full context
    C-->>W: code / spec written
    opt worker is unsure
        W->>D: jarvis wo ask "{one atomic question}"
        D->>N: neo_tick drains the question
        alt Neo answers
            N-->>D: verdict
            D->>W: next turn: --resume {uuid} with the answer
        else Neo escalates
            N-->>D: escalate
            D-->>U: inbox + attention: jarvis neo answer {qid}
            U->>D: jarvis neo answer {qid} "…"
            D->>W: next turn with the user's answer
        end
    end
    opt worker records a judgment call
        W->>D: jarvis wo assume "…"  (see Gates and assumptions)
    end
    W->>GH: push branch, open PR
    W->>D: jarvis wo finish --pr {url} --summary … --evidence …
    D->>D: settle_turns → settle_work_order
    alt validation enabled
        D->>D: status validating (see Validation rounds)
    else pending assumptions
        D-->>U: needs_review → jarvis wo review {id}
    else PR on record
        D->>D: waiting_pr_merge (no attention flag)
        D->>GH: poll_pull_requests every ~2 min
        GH-->>D: MERGED → completed  /  CLOSED → needs_review
    end
```

## Feedback while it runs

The user never writes into a worker's session directly. Feedback goes in as a queued
message, and the daemon delivers it as the worker's next turn.

```mermaid
sequenceDiagram
    actor U as User
    participant J as Jarvis
    participant D as jarvisd
    participant W as Worker

    U->>J: "tell it to use the existing helper"
    J->>D: jarvis wo send {wo-id} "…"  (wo_messages: queued)
    alt a turn is in flight
        Note over D: worker_session.busy: one turn at a time,<br/>the message waits
    end
    D->>W: deliver_messages: claude -p --resume {uuid} -- "{message}"
    W-->>D: the turn's result is the worker's reply (record_agent_reply)
    D-->>J: visible on jarvis wo show
```

## When a turn dies

```mermaid
flowchart TD
    T[Turn ends] --> R{Latest turn row}
    R -- "usage limit refusal" --> P1["Paused: retry at the reset time<br/>(same prompt, up to 8 times)"]
    R -- "API error ≥ 500" --> P2["Paused: back off 60s → 1200s<br/>(continuation nudge, up to 5 times)"]
    R -- "other failure" --> F["failed + attention<br/>user: jarvis wo retry"]
    R -- "done + queued messages" --> NX[Next turn this tick]
    R -- "done + finish" --> S[Settle: validating / needs_review /<br/>waiting_pr_merge / completed]
    R -- "done, no finish" --> IR["needs_review:<br/>idle without jarvis wo finish"]
    P1 & P2 --> NX
```
