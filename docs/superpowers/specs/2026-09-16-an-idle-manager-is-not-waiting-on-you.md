# An idle manager is not waiting on you

GitHub issue #264. A `kind='manager'` work order — the addressee a feature order routes
its messages through — writes no code and opens no pull request. It acts on a message
and ends its turn; between messages there is nothing for it to do. That is its designed
steady state, not a question.

The OS parked it in `waiting_input`.

## What that cost

wo-bf1b6def sat for eleven hours reading **"Waiting on you"** with nothing for a person
to answer. Clearing it made it worse: a nudge bought one turn of the manager saying
nothing was needed, and the reconciler parked it straight back — 0.37 USD a lap. And
`jarvis wo resume-auto`, asked what was holding it, fell through to its last branch:
*"nothing else accounts for it: an unanswered permission prompt is what is left"* —
impossible, since the fleet runs `auto`, where by design nothing can prompt. The OS gave
a confident wrong explanation for a state it had created itself.

## Why suppressing the flag was never enough

The attention **flag** had already been exempted by kind, in two places
(`invariants.true_blockers` and `invariants.parked_reason`). It did not help, because
six other surfaces re-derive meaning from the **status alone**:

| Surface | What it did with `waiting_input` |
|---|---|
| `timeline.STATUS_LABEL` | rendered "Waiting on you" |
| `ui.STATUS_META` | `warn` tone, "waiting on you" |
| `ui.FEATURED_STATUSES` | gave it a row in the dashboard's needs-me strip |
| `ops.waiting_on` | fell through to "an unanswered permission prompt" |
| `ops.resume_in_auto` | offered the nudge that re-created the state |
| `invariants.BLOCKED_STATUSES` | scanned it for user blockers every reconcile tick |

This is kn-cffc8905's trap — *"settling a work order to `waiting_input` WITHOUT attention
does not hold"* — paid a third time. A status that lies is the defect; the carve-outs
were the interest on it.

## The fix

A real work-order status, `idle`, between `running` and `waiting_input` in
`project_store.WO_STATUSES`. `Daemon.settle_work_order`'s manager branch parks there.

Both `kind == "manager"` carve-outs are **gone**, and removing them fixes a bug the
first one was hiding: a manager reaches `waiting_input` only by *asking* — a gate, or
`jarvis wo ask` — so the one case the exemption suppressed was the case that most needed
the user, Neo handing that question back.

### Where `idle` sits in each tuple, and why

| Tuple | In? | Why |
|---|---|---|
| `OPEN_STATUSES` | yes | the manager is alive for its feature's whole life |
| `ACTIVE_STATUSES`, `SLOT_STATUSES` | no | no turn in flight; nothing is drawn |
| `RETRY_SWEEP_STATUSES` | **yes** | a manager's turn refused for the usage limit settles back to `idle` holding a due pause. Skipping it strands the one order a feature routes everything through — issue #259 one level down |
| `BLOCKED_STATUSES`, `MESSAGE_STUCK_STATUSES` | yes | for exactly **one** blocker and no other: a message the user or the bus sent that the manager will never see. A feature routes everything through its manager, so a message rotting there strands the feature silently |
| `PARKABLE_STATUSES` | no | "nothing is in flight" is the *definition* of `idle`, not news about it. This absence is what replaced `parked_reason`'s carve-out |
| `PR_REPAIR_STATUSES`, `FORCEABLE_STATUSES` | no | a manager opens no pull request and delivers nothing to re-judge |

`ops.waiting_on` answers `manager_idle`, and `ops.NUDGE_IS_WRONG` puts it beside
`message_stuck`: the two answers where a nudge is *actively wrong* rather than merely
useless, refused regardless of permission mode.

## Migration

No schema change — a status is a `TEXT` column plus a tuple and an `assert`. Managers
already parked in `waiting_input` are re-statused by `Daemon.settle_work_order` on the
first tick, which is why `idle` is in `settle_turns`'s sweep. That pass runs **before**
`check_invariants` in the same tick, so no carried-over manager is flagged in the window
between the two.

### What the migration must not touch

That branch used to be a no-op for a manager already parked; it is now a **rewrite plus
an unflag**, and `waiting_input` is the only carrier of the fact that a manager *asked*.
Two guards, both added in review round 1:

**The predicate, not the branch order.** The `elif` above the manager branch reads
`store.pending_approvals`, which is two thirds of the question and looks like all of it:
it excludes `awaiting_case`, the gate request a worker files by *running* the command
before arguing it — and `gates.file_request` parks a work order in `waiting_input` down
that road too. Missing it used to cost nothing, because the branch wrote `waiting_input`
either way. So the manager branch guards on `invariants.something_is_out`, which is
`pending_approvals or held_approvals or awaiting_neo`, shared with
`end_wait_if_nothing_is_out` so the two cannot drift (kn-4ea33fe6). An escalated *question*
is already covered without the guard — `neo_store.OPEN_Q_STATUSES` includes `escalated`,
so `awaiting_neo` still answers for it — and a turn that died on auth never reaches this
branch at all, because `settle_work_order` returns early on a `failed` turn. Both are
pinned anyway: nothing pinned them before, and both are one edit away from breaking.

**The unflag is re-derived, not assumed.** Clearing attention unconditionally would drop a
blocker that survives the move — a pending assumption is owed whatever the status. So it
reads `true_blockers` on the row **as it now is**, after the `set_status`, and clears only
when that is empty. Reading the pre-move row instead would re-derive the old status's
"worker is waiting on your input" and never clear at all. The clear itself cannot simply
be deleted: INV-ATTENTION-PHANTOM clears flags only on *terminal* rows, so a flag raised
under the old status would outlive the status it was derived from for ever.
