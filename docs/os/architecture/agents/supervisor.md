---
type: Jarvis Architecture Concept
title: "Agents: supervisor and self-heal"
description: How burning turns and health findings become alarms, how the supervisor acks, escalates or proposes a remedy, and how a remedy is approved through the self_heal gate.
resource: ../../../superpowers/specs/2026-09-02-supervisor-health-and-healing.md
tags: [architecture, agents, supervisor, alarms, diagrams]
---
# Agents: supervisor and self-heal

The supervisor is **not** a work order. It is a one-shot structured headless call
(`supervisor.review`) on the daemon's supervisor thread, with a neutral working
directory. It judges one alarm at a time. A second call, the health sweep
(`supervisor.review_health`), judges orders against the armed probes (`probes.py`). It
can only act through `remedies.py`, a closed registry, and only after Neo approves a
`self_heal` gate.

```mermaid
sequenceDiagram
    autonumber
    participant D as jarvisd
    participant S as Supervisor
    participant N as Neo
    participant R as remedies.apply
    participant W as Work order
    actor U as User

    alt burning turn / rewrite tax
        D->>D: check_burning_turns, check_rewrite_tax → alarm raised
    else health sweep
        D->>S: health_tick: review_health vs armed probes
        S-->>D: finding → alarm + attention on the carrier WO
    end
    D->>S: supervisor_tick: claim_next_alarm → review
    alt ack
        S-->>D: alarm acked, info line in the inbox
    else escalate
        S->>N: kind=alarm
        alt Neo answers
            N-->>D: alarm acked with Neo's note (advice on the WO)
        else Neo escalates
            N-->>U: attention + inbox
        end
    else propose a remedy
        S->>D: remedies.propose: self_heal approvals row + Neo kind=approval<br/>(the catalog's allow-list may refuse → user flagged)
        D->>N: gate review
        alt approved
            N-->>D: approved
            D->>R: remedy_tick: apply only with a live grant
            R->>W: nudge / unblock / update_branch / force_rejudge / …
        else escalated
            N-->>U: jarvis gate approve|deny {id}
        end
    end
```

`self_heal` is the one gate that no command classifies into. It is mandatory, whatever
the project's gate config says. Its "command" is a rendered intent that nothing
executes. See [Privileged-action gates](/architecture/gates.md).
