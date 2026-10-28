---
type: Jarvis Architecture Concept
title: "Agents: gates and assumptions"
description: How a worker's privileged command and its recorded assumptions reach Neo, what each Neo question kind is for, and when the user is asked.
tags: [architecture, agents, gates, neo, diagrams]
---
# Agents: gates and assumptions

Neo stands in for the user. Each thing an agent needs decided becomes a **question** in
`neo.db`, and its `kind` names the persona that answers it and the handler that delivers
the verdict (`Daemon._neo_drain`).

| Kind | Filed by | Verdict delivered to |
|---|---|---|
| `question` | a worker: `jarvis wo ask` | the worker's next turn; escalation → user |
| `approval` | a gated command (hook), auto-merge, a supervisor remedy | `_deliver_gate_verdict` → approvals row (+ worker message) |
| `plan` | a planner: `jarvis fo plan` | `_deliver_plan_verdict` → release / back to planner / user |
| `assumption` | `autoreview`, on a pending assumption | `_deliver_assumption_verdict` → accept, or escalate to user |
| `alarm` | the supervisor | `_deliver_alarm_verdict` → advice on the WO, or user |
| `triage` | `jarvis bug report` at critical/blocker | `issues.settle_triage` → label + route |

When `os.neo.panel` is enabled (it is off by default), `question` and `approval` are
answered by a seat panel instead of a single call. A `premise` seat answers likely
classifier false positives alone. Every real privileged action goes through the full
panel and the `chair`.

## A privileged command (gate)

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker
    participant H as PreToolUse hook
    participant D as jarvisd
    participant N as Neo (gate reviewer)
    actor U as User

    W->>H: Bash: gh pr merge … (or jarvis gate request)
    H->>H: gates.classify (quoted spans are data)
    H->>D: file_request: approvals row + Neo question kind=approval
    H-->>W: DENY: "end your turn, the verdict will arrive"  (WO waiting_input)
    Note over H: the hook never waits: a review takes minutes
    D->>N: neo_tick
    alt approve / deny / dismiss
        N-->>D: verdict
        D->>W: next turn: the verdict as a message
        W->>H: retries the byte-identical command
        H-->>W: allow (consume_grant, dismiss = never expires)
    else escalate
        N-->>D: escalate
        D-->>U: attention: jarvis gate approve|deny {id}
        U->>D: decision → delivered to the worker the same way
    end
```

While Neo holds the gate, the user's attention is not spent: `_waiting_on_neo_gate`
suppresses the flag.

## Assumptions and Neo's auto-review

A worker records a judgment call with `jarvis wo assume`. The default reviewer is the
user (`jarvis wo review`). A project can hand routine ones to Neo with
`validation.auto_review`. Neo can only **accept** or pass the decision to the user.

```mermaid
sequenceDiagram
    participant W as Worker
    participant D as jarvisd
    participant N as Neo
    participant B as Bus
    actor U as User

    W->>D: jarvis wo assume "…"
    alt held (auto-review off, high stakes, secret evidence, …)
        D-->>U: stays on jarvis wo review
    else early pass: worker still running
        D->>N: kind=assumption (decision record + spec section)
        N-->>D: provisional verdict
        opt Neo objects
            D->>B: assumption_objection reviewer → implementor
            B->>W: delivered while it still works
        end
        Note over D,N: re-confirmed with Neo once the WO is needs_review
    else WO is needs_review
        D->>N: kind=assumption
        alt accept
            N-->>D: accept → ops.accept_assumption<br/>none left → may open a validation round
        else not accepted
            N-->>D: → escalated to the user
            U->>D: jarvis wo review {id} [--reject] --feedback "…"
        end
    end
```
