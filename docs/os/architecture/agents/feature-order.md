---
type: Jarvis Architecture Concept
title: "Agents: feature order"
description: How the planner and its seats, Neo, the daemon, the project manager and the child workers interact across a feature order's life.
tags: [architecture, agents, feature orders, diagrams]
---
# Agents: feature order

A feature order (`jarvis fo create`) is an ask too big for one session. The OS plans it
itself, and no new scheduler is involved. The children are ordinary work orders joined
by `depends_on` edges. States and validator rules:
[Feature orders](/architecture/feature-orders.md).

## Plan, review, execute

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant J as Jarvis
    participant D as jarvisd
    participant P as Planner (WO kind=planner)
    participant S as Seats: jarvis-architect,<br/>jarvis-test-lead (read-only)
    participant N as Neo (plan reviewer)
    participant M as Project manager
    participant C as Child workers<br/>(--agent fo-id)

    U->>J: "build feature F in project X"
    J->>D: jarvis fo create X "F" -d "{whole ask}"  (FO pending)
    D->>P: plan_features: one planner WO  (FO planning)
    P->>S: Task: architecture / test strategy questions
    S-->>P: findings (no writes, no shell)
    P->>P: write the spec (with an Agent profile appendix) + plan.json
    P->>D: jarvis fo plan {fo-id} --from-file plan.json
    Note over D: plans.parse_plan + spec_problems:<br/>cycles, unknown ids, cap, standalone briefs,<br/>one spec_section per child
    D->>D: planner settled  (FO plan_review)
    D->>N: kind=plan, a skeleton question (ask + one line per child)
    alt release, and fewer than 8 children
        N-->>D: release
    else reject
        N-->>D: reason → delivered to the planner, FO back to planning
        D->>P: next turn: revise
    else escalate (forced at ≥ 8 children)
        N-->>D: escalate
        D-->>U: attention on the FO → jarvis fo approve {id} [--reject]
    end
    D->>D: create_plan_children in dependency order<br/>+ manager WO if validation on  (FO executing)
    D->>D: build the feature agent type from the spec's Agent profile
    loop every child, as its depends_on edges clear
        D->>C: dispatch: --agent {fo-id}, section file + design snapshot
        C-->>D: ordinary work-order flow (see Work order)
    end
    D->>D: settle_features: all children completed
    alt feature validation on
        D->>D: FO validating (see Validation rounds)
    else
        D->>D: FO completed, agent type deleted
    end
```

## The project manager's mailbox

The manager is the only agent that works on a feature *after* its plan is released.
It is a work order that sleeps in `idle` and takes no concurrency slot. Envelopes are
its only input.

```mermaid
flowchart LR
    R1["Validation panel<br/>(role: reviewer)"] -- review_feedback --> BUS{{bus.resolve}}
    R2["Child worker<br/>(role: implementor)<br/>jarvis wo defer"] -- deferral_request --> BUS
    R3["jarvisd<br/>(role: reconciler)"] -- children_landed --> BUS
    BUS -- "manager WO live" --> M["Project manager"]
    BUS -- "no manager: deferral" --> BL["Backlog item<br/>(handled_by_router)"]
    BUS -- "manager terminal" --> X["undeliverable +<br/>attention on the FO"]
    M -- "jarvis wo create --parent" --> NC["New child WOs"]
    M -- "jarvis fo submit" --> VR["Next feature round"]
```

## When a child fails

A failed child fails its feature, because `failed` is settled. If the child later
recovers, the OS reopens the feature within a tick. If the child is genuinely dead,
`jarvis fo resume <id> --fix "…"` marks it superseded and files the fix as a new child.
