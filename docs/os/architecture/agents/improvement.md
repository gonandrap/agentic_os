---
type: Jarvis Architecture Concept
title: "Agents: improvement order"
description: How an analyst turns an observation of OS misbehaviour into findings, and how the user's accept or reject files orders, knowledge and Neo learnings.
tags: [architecture, agents, improvement orders, diagrams]
---
# Agents: improvement order

An improvement order (`jarvis io create`) is filed when the OS **misbehaves** and the
user wants the cause. It is a feature order with `kind=improvement`, carrying one
`analyst` work order (read-only, `dispatch.READ_ONLY_KINDS`). The analyst is told to
argue *against* the obvious fix. It never opens a PR. Unlike an investigation, it does
not settle itself: **nothing is filed until the user decides**.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant J as Jarvis
    participant D as jarvisd
    participant A as Analyst (read-only WO)
    participant O as ops
    participant KB as Knowledge base
    participant N as Neo learnings

    alt by hand
        U->>J: "the OS keeps doing X"
        J->>D: jarvis io create {project} "…" -d "{observation}" --ref {id|#n|url} …
    else from jarvis fix
        O->>D: no remedy claims the gap class → create_improvement_order
    end
    D->>A: plan_features: analyst WO
    A->>A: resolve each --ref (an unresolvable ref is itself a finding)
    A->>O: jarvis io report {io} --from-file report.json
    O->>O: findings.parse_report → FO plan_review, one attention item
    O-->>U: "3 findings: awaiting you"
    loop each finding
        alt accept
            U->>O: jarvis io review {io} --accept {key}
            O->>KB: learn add (topic os-failure-mode): the root cause
            O->>D: file each proposed order (WO / FO) with a back-link
        else reject
            U->>O: jarvis io review {io} --reject {key} --feedback "why"
            O->>N: rejection becomes a Neo learning
        end
    end
    O->>D: no finding pending → FO completed
```

A send-back can reach the analyst as its next turn, and it may resubmit while the order
is in `plan_review`.
