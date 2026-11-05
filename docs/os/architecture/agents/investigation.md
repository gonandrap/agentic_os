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
        O->>O: verdict_coverage: one seat, is a gap named with no entry?
        O-->>I: first miss returned, nothing stored
        O->>GH: duplicate check: issues + live orders
        alt duplicate found
            O->>O: rewritten to ALREADY_TRACKED (classified_by ops)
        else
            O->>GH: bugreport.report_bug(expedite=False): file only
            Note over GH: critical/blocker → Neo triage<br/>(see Bugs and releases)
        end
    else WAITING_ON_USER (user_owes)
        O-->>U: attention on the investigation<br/>(cleared once the subject is clear)
        Note over O: on a monitoring order, behaviour_correct true settles it unverified, no attention
    else SCRIPT_WRONG (script_fix; Morpheus-opened only)
        O->>O: morpheus.rewrite within MAX_FIX_CYCLES,<br/>script_fix + root_cause as the brief
        O-->>U: past the cycles: attention + inbox naming jarvis morpheus rewrite
    else TRANSIENT (unsticks)
        O->>O: on a monitoring order: watch once more; a second TRANSIENT settles it unverified
    else ALREADY_TRACKED (duplicate_of)
        O->>O: nothing further
    end
    O->>D: FO completed, investigation_verdict event on the subject,<br/>investigator closed out
```

A GAP is filed **without** expediting, so it does not jump the queue. The user picks it
up with `jarvis issues start` unless Neo's triage confirms critical/blocker. If the
tracker cannot be reached, the investigation raises attention and shows the retry
command.

Every verdict, whatever its classification, carries `other_gaps`: every gap beyond the root
cause, or an explicit `[]`. `ops.submit_verdict` posts each entry through the bus as a
deferral on the investigator work order, and the router files it as a `proposed` backlog
row with an origin link. It is never a tracker bug and never dispatched.

Before filing, `verdict_coverage` asks one seat whether the text names a gap with no
entry in `proposed_fix` or `other_gaps`. A first miss returns to the session naming the items. A second submission
settles with any remaining miss stored as `coverage_miss`. A seat with no usable answer
takes the same retry path as an unreachable tracker: nothing filed, order still `planning`.
A quote the verdict does not contain never blocks.
