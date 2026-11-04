# A standing meter gap is one condition (issue 1543)

## The problem

Five `cost_residual_high` escalations reached the user in 3.4 days, on wo-a0ca6a40,
wo-033d60db, wo-8471158d, wo-ea726f56 and wo-c9337339, with no count and nothing to do.

Root cause: these kinds describe the machine, but `Daemon.check_meter_residual` hung each on
`store.latest_settled_order()` and deduped only inside the current meter window, so every
window with the gap raised an independent alarm. `supervisor.build_evidence` showed the alarm
and the carrier's history, nothing about earlier occurrences, so the supervisor correctly
said the carrier could not explain it and escalated each time. wo-c9337339's residual was
-$80.09 (-60%): the gap goes both ways, which points at the accounting, not invisible usage.

## The fix

### One condition

Rows of one kind form a chain while neighbours are under `METER_STANDING_GAP_SECONDS`
(7 days) apart, and the newest is that close to now (`ProjectStore.standing_alarms`).
Consecutive windows cannot define it: production raised on 5 of about 16 windows. The count
and first-seen are derived from `wo_alarms`, so no schema change, and rows from before this
release count (Q3395). A row states both in its reason and in its `cost_alarm` event.

### What each occurrence does

- 1: inbox line, then supervisor review. This line is the user's only one.
- Below `METER_STANDING_OCCURRENCES` (3): supervisor review, no inbox.
- From 3: an `informational` row. No model call, no inbox.

For these kinds a verdict is written on the row and goes no further: no Neo question, no
attention flag on the carrier, no inbox line, no remedy (`supervisor._apply`). The packet
gains a section with prior occurrences and their verdicts (`supervisor._standing_lines`).

### The diagnostic

From occurrence 3, `cost_residual_high` is filed once through `bugreport.report_bug` at
`high`, with the `Bug filed` inbox line off (Q3394). It takes the steward path as a filed
bug, so `os.autonomy.self_dispatch` off means filed and nothing started. A
`cost_residual_diagnosed` informational row records the filing; one inside the chain stops a
second. A failed filing records nothing and retries on the next 30-minute pass.

`cost_outside_spend_high` files nothing (Q3396): outside spend is the user's own sessions,
with no accounting fix to diagnose. Its repeats are counted, visible in `jarvis alarms`.
