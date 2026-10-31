---
type: Jarvis Architecture Concept
title: "Agents: investigation order"
description: How an investigation order is opened by the user or the stuck sweep, how its read-only investigator works, and what each of the four verdicts does.
resource: ../../../superpowers/specs/2026-09-27-investigation-orders.md
tags: [architecture, agents, investigation, diagrams]
---
# Agents: investigation order

An investigation asks why **one** order is not moving. It is a feature order with
`kind=investigation`, and it carries a single `investigator` work order. The
investigator never writes code. Its hooks (`hooks.investigator_write_decision`,
`investigator_bash_decision`) refuse every write except `verdict.json` at the worktree
root. They also refuse every mutating command except four: `wo ask`, `wo assume`,
`learn add` and `investigate verdict`. The order settles itself.

## Opening, working, settling

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant D as jarvisd
    participant I as Investigator (read-only WO)
    participant O as ops.submit_verdict
    participant GH as Tracker (GitHub)

    alt by hand
        U->>D: jarvis investigate {wo|fo|io-id} --why "what I saw"
    else stuck sweep (origin fleet_health)
        D->>D: stuck_tick → stuck.assess<br/>(daily cap, cooldown, fingerprint, skipped when paused)
    else validation seats killed
        D->>D: _killed_investigation on the round's WO
    end
    Note over D: one live investigation per subject,<br/>never an investigation of an investigation
    D->>I: plan_features: investigator WO, the "why" verbatim<br/>(investigation_model)
    I->>I: jarvis wo show / validation show / learn search,<br/>git log/diff, gh pr view, Serena (reads only)
    I->>I: Write verdict.json
    I->>O: jarvis investigate verdict {inv} --from-file verdict.json
    O->>O: verdicts.parse_verdict (fields required and forbidden per class)
    alt GAP (proposed_fix)
        O->>GH: duplicate check: issues + live orders
        alt duplicate found
            O->>O: rewritten to ALREADY_TRACKED (classified_by ops)
        else
            O->>GH: bugreport.report_bug(expedite=False): file only
            Note over GH: critical/blocker → Neo triage<br/>(see Bugs and releases)
        end
    else WAITING_ON_USER (user_owes)
        O-->>U: attention on the investigation<br/>(cleared once the subject is clear)
    else SCRIPT_WRONG (script_fix; Morpheus-opened only)
        O->>O: morpheus.rewrite under MAX_SCRIPT_REWRITES,<br/>script_fix + root_cause as the brief
        O-->>U: cap spent: attention + inbox naming jarvis morpheus rewrite
    else TRANSIENT (unsticks) / ALREADY_TRACKED (duplicate_of)
        O->>O: nothing further
    end
    O->>D: FO completed, investigation_verdict event on the subject,<br/>investigator closed out
```

A GAP is filed **without** expediting, so it does not jump the queue. The user picks it
up with `jarvis issues start` unless Neo's triage confirms critical/blocker. If the
tracker cannot be reached, the investigation raises attention and shows the retry
command.
