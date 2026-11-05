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
release count (Q3395). A row states both in its reason.

### What each occurrence does

- 1: inbox line, then supervisor review. This line is the user's only one.
- Below `METER_STANDING_OCCURRENCES` (3): supervisor review, no inbox.
- From 3: an `informational` row. No model call, no inbox.

For these kinds a verdict is written on the row and goes no further: no Neo question, no
attention flag, no inbox line, no remedy (`supervisor._apply`). The packet
gains a section with prior occurrences and their verdicts (`supervisor._standing_lines`).

### The diagnostic

From occurrence 3, `cost_residual_high` is filed once through `bugreport.report_bug` at
`high`, with the `Bug filed` inbox line off (Q3394). It takes the steward path as a filed
bug, so `os.autonomy.self_dispatch` off means filed and nothing started. A
`cost_residual_diagnosed` informational row records the filing; one inside the chain stops a
second. A failed filing records nothing and retries on the next 30-minute pass.

`cost_outside_spend_high` files nothing (Q3396): outside spend is the user's own sessions,
with no accounting fix to diagnose. Its repeats are counted, visible in `jarvis alarms`.

### No carrier (wo-87e007eb, Q3553)

`project_store.MACHINE_WIDE_KINDS` (the meter and cache-TTL kinds; `check_cache_ttl` has no
causal order either) write `wo_alarms.wo_id` NULL, no `wo_events` row and no inbox `wo_id`, and
need no settled order to raise. The review gets subject kind `machine`; its verdict stays on
the row. The rebuild to nullable runs once on open, and also takes existing rows off their
carriers and deletes those rows' events there. `proposed` rows keep their carrier, for their
gate. Attention flags already up stay for the user's ack.

The inbox line states the user's action, or says none is owed. A cache-TTL defect owes a
`jarvis bug report`, since nothing files it. Recurrence stays meter-only (kn-9a7e536a).
