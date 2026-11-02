---
type: Jarvis Architecture Concept
title: "Agents: bugs and releases"
description: How a bug filed by any agent is triaged by Neo, when it becomes a work order, and how an expedited or confirmed fix ships a release once it lands.
tags: [architecture, agents, bugs, releases, diagrams]
---
# Agents: bugs and releases

Any agent can file an OS bug with the `report-jarvis-bug` skill (`jarvis bug report`).
**The priority routes it.** Neo re-assesses a claim of `critical` or `blocker`.
`--expedite` is a scheduling decision that only the user can make: inside a work order
(`JARVIS_WO_ID` set) it is ignored. A work order's filing is never left for the user: the
steward decides it at every priority, and a queued one starts when `steward.drain` finds room.

```mermaid
sequenceDiagram
    autonumber
    participant F as Filer (any agent or user)
    participant I as issues.route_filing
    participant GH as Tracker (GitHub)
    participant N as Neo (triage)
    participant D as jarvisd
    participant W as Fix worker
    actor U as User

    F->>I: jarvis bug report "…" -p {priority} [--expedite]
    I->>GH: create issue + priority label
    opt --expedite (user only)
        I->>D: work order now (expedited, no rating on it yet)
    end
    alt low / medium / high, filed by a work order
        I->>D: steward.gate(bug_agent) → dispatch now / queue (drain re-offers) / drop
    else low / medium / high, filed by the user
        Note over GH: waits on the tracker
        U->>D: jarvis issues start {url|#n}
    else critical / blocker
        I->>N: kind=triage
        alt confirmed, or a work order's filing downgraded
            N-->>D: settle_triage → steward.gate at the settled level
        else the user's filing downgraded / unconfirmed
            N-->>U: inbox: jarvis issues start {url}
        end
    end
    D->>W: ordinary work-order flow (+ validation)
    W-->>GH: PR merged → issue closes itself
    D->>D: sync_issues: closed + (expedited or confirmed priority)<br/>→ ensure_release (batched, held while main is red)
    D->>D: release_tick → release.maybe_restart, verify_on_boot
```

Several fixes that land close together ship as **one** release. A red `main` holds
the release instead of shipping it.
