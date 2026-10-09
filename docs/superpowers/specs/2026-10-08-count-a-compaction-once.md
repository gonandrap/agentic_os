# Count a compaction once

Issue 989, wo-d47ebc7b. Spec for `bill._turn_items`, `bill._check_calls` /
`bill.reconcile` and `budget._worker_row`.

## The problem

A cold-conversation compaction is written down TWICE, and two of the three money
surfaces add both copies up.

`worker_session.compact` (src/jarvis/worker_session.py:557) launches the `/compact`
prompt as an ordinary turn, `kind='compact'` (`project_store.COMPACT_TURN`,
src/jarvis/project_store.py:378), so the CLI's result envelope lands on a `wo_turns`
row like any turn's: `cost_usd`, `by_model`, the four token classes. Then
`worker_session._record_compaction` (src/jarvis/worker_session.py:626-663) writes the
SAME envelope again as an `agent_calls` row of kind `compaction`
(`agent_usage.COMPACTION`, src/jarvis/agent_usage.py:83).

Both are correct records. The defect is that every surface summing the worker half out
of `wo_turns` and the Jarvis half out of `agent_calls` charges the order for one
compaction twice.

Measured on wo-be05ab99: $1.575 / 259k tokens appear in BOTH halves, and
`jarvis cost wo-be05ab99` reports an order total of 39.62 where the true figure is
38.04 — a 4.2% overstatement on one compaction, growing linearly with the number of
relaunches a long order survives.

Root cause named, and it is not the double write: it is that `compact` was given a
`wo_turns` row of the same KIND-space as worker turns, and nothing makes a reader of
that table declare which kinds it means. `fleetcost` declared it
(`WORKER_TURN_KINDS`, src/jarvis/fleetcost.py:65-67); `bill` and `budget` never did.
The structural fix — a single accessor nobody can bypass — is out of scope here; this
spec repairs the two readers.

### Site 1: `bill._turn_items` (src/jarvis/bill.py:436-527)

It iterates EVERY turn row, and for each recorded one (a) adds its `by_model` figures
into the `recorded` accumulator (bill.py:470-472) and (b) emits worker `Item`s through
`_agent_items` (bill.py:482). A `compact` row therefore produces a `worker` actor line
and a `worker` child under its turn line, while `_call_items` (bill.py:715-768) emits
the `agent_calls` compaction item — described "compacting a cold conversation",
src/jarvis/agent_usage.py:141 — under `jarvis`. One compaction, two items, and
`payload["total"]` is the sum of the item list.

The `recorded` accumulator is the second, quieter half. `recorded` is subtracted from
`session.total` to produce the transcript GAP line (bill.py:491-496), and
`usage.read_session` cannot see a compaction at all — it writes no assistant message
(`claude_cli.COMPACT_PROMPT`; the reason `_record_compaction` exists). So
`session.total` excludes the compaction while `recorded` includes it, `recorded > whole`
by the compaction's size, and `max(0, …)` silently swallows a real transcript gap of up
to that size. An order that has both compacted and lost a result JSON loses the gap
line entirely.

Three derived lists in the same function are computed off the unfiltered rows and
mislabel the gap when a compact turn is in flight or is the only turn on record
(bill.py:502-519):

* `live` and `unrecorded` — an in-flight compact row is `state == 'running'` and
  `recorded` false, so it counts in both. It inflates `calls=len(unrecorded)` on the gap
  item, and a real unrecorded worker turn beside it flips the branch condition
  `live and len(live) == len(unrecorded)`, printing "turns with no result JSON left"
  (data loss) where "turn still running" is the truth, or the reverse.
* `if not turn_rows` — an order whose ONLY rows are compact turns is non-empty here, so
  the "no turns on record at all" branch is skipped and the transcript is labelled
  "turns with no result JSON left".

### Site 2: `budget._worker_row` (src/jarvis/budget.py:150-162)

```sql
SELECT COALESCE(SUM(cost_usd), 0) AS c, … FROM wo_turns WHERE wo_id=?
```

Unfiltered by kind. So `Spend.worker_usd` (src/jarvis/budget.py:118) carries the
compaction, `Spend.jarvis_usd` carries it again via `central.wo_call_cost`
(src/jarvis/budget.py:147, every `agent_calls` row), and `Spend.total_usd` —
the number a `jarvis wo budget` ceiling stops work on, and the number handed to
`--max-budget-usd` as `cap - spent` — double-counts it. A compacted order is parked in
`budget_exhausted` before it has spent its cap, by the size of its compactions.

### Site 3 (the guard that was not there): `bill.reconcile`

`reconcile` (src/jarvis/bill.py:1397-1435) prints "every line below adds up" and could
never have caught this. Its three claims are that the by-turn fold, the by-actor fold
and every parent line sum to the total — and all of them are folds of ONE item list, so
a duplicate item is invariant under all three. Its own docstring says so: "They cannot
fail while both views are folds of one item list." The bill shipped 4.2% high, balanced.

## The fix

The compaction stays on the JARVIS half (`agent_calls`, kind `compaction`) and leaves
the WORKER half. SETTLED AND PRECEDENTED — stated, not re-opened:

1. `agent_usage.py:77-82` already declares `COMPACTION` "an OS kind and not a worker
   one: the worker did not ask for it and learns nothing from it".
2. `compaction_payoff` reads the compaction's cost from `agent_calls`
   (src/jarvis/compaction_payoff.py:388-389, `WHERE kind = 'compaction'`); the net
   saving is computed against it (compaction_payoff.py:231).
3. `fleetcost.WORKER_TURN_KINDS` (src/jarvis/fleetcost.py:67) ALREADY excludes
   `COMPACT_TURN` from worker turns while adding `OrderStats.os_cost_usd`. fleetcost is
   already right. This fix makes `bill` and `budget` agree with it rather than
   introducing a new opinion.

REJECTED: drop the `agent_calls` row and keep the turn. It would delete
`compaction_payoff`'s cost source, report the saving gross, and move OS spend onto the
worker's line — the one distinction the actor split exists to draw.

REJECTED: filter at write time (stop writing one of the rows). Both rows carry things
the other does not — see "What is NOT changed" — and a write-time fix re-prices nothing
already on disk.

### 1. `bill._turn_items`: one filtered sequence, used throughout

`bill` imports `COMPACT_TURN` from `project_store` (as `fleetcost` does,
src/jarvis/fleetcost.py:56) and `_turn_items` derives, as its first statement, the
worker-turn subsequence:

```python
worker_rows = [r for r in turn_rows if r.get("kind") != COMPACT_TURN]
```

`kind` is on every row (`ops._turn_row`, src/jarvis/ops.py:11990). From that point
`worker_rows` is the ONLY sequence the function reads: the item loop, the `recorded`
accumulator, `_subagents_by_turn`, `live`, `unrecorded` and the `if not turn_rows`
emptiness test. One filtered binding rather than a `continue` in the loop, because the
defect in the three derived lists is exactly that each of them re-derived "the turns"
independently; a `continue` fixes one of four readers and leaves three.

`turn_rows` itself is NOT filtered and is NOT mutated — it is returned on the payload
(`bill.for_work_order`, src/jarvis/bill.py:1201) and is the per-turn table on
`jarvis cost`, the bill page and `jarvis inspect`. A compact turn keeps its row, its
timings, its context figures and its `cost_usd` there. What changes is that no WORKER
item is charged from it.

`row["subagent_cover"]` (bill.py:477-480) is set inside the item loop, so a compact row
no longer gets the key — correct, and §3 depends on it.

USER-VISIBLE OUTCOME, stated because it is the thing a reviewer will check:
`_turn_locator` (bill.py:771-790) is unchanged, so the `agent_calls` compaction item
still lands on the compact turn's own seq (`_call_items` locates by `ts`, bill.py:760).
The by-turn view keeps ONE "turn N" line for the compaction, with the same tokens and
the same dollars as before, attributed to `jarvis` — "compacting a cold conversation" —
instead of to `worker`. No turn disappears from the table; one turn changes actor and
the order total drops by the compaction.

### 2. `budget._worker_row`: filter the SUM, not the rows

The `cost_usd` aggregate becomes conditional; the `WHERE` clause and the staleness
aggregate beside it are untouched:

```sql
SELECT COALESCE(SUM(CASE WHEN kind <> ? THEN cost_usd ELSE 0 END), 0) AS c,
       COALESCE(SUM(CASE WHEN COALESCE(json_extract(usage_json,'$.usage_v'),1) < ?
            AND outfile IS NOT NULL AND outfile <> ''
            AND state IN ('done','failed') THEN 1 ELSE 0 END), 0) AS stale
FROM wo_turns WHERE wo_id=?
```

Parameters become `(COMPACT_TURN, USAGE_SCHEMA_VERSION, wo_id)` — the new placeholder is
FIRST, and the existing two keep their order. `budget` imports `COMPACT_TURN` from
`project_store` beside its existing imports (src/jarvis/budget.py:66-70).

Why `CASE` in the aggregate and not `AND kind <> ?` in the `WHERE`: the staleness
sub-query must keep seeing every row. It drives the lazy re-derivation in
`budget.spent` (src/jarvis/budget.py:133-143), which calls `ops._turn_rows` — a repair
over ALL turns, compact ones included (their envelopes feed the delta chain
`_turn_usage` derives each turn against). Excluding compact rows from the `stale` count
would leave a stale compact envelope un-repaired, and the whole statement stays one
indexed scan either way, which is the contract in the docstring.

`Spend`, `spent`, `Spend.total_usd` and every caller are unchanged: the number they add
up is simply no longer inflated.

### 3. A cross-check that can actually fail

New check in `_check_calls` (src/jarvis/bill.py:1462-1509), reached from `reconcile` at
bill.py:1434. It tests the SHAPE of the defect — a compaction charged on both halves —
not a number, and it reads only what is already on the payload:

For each `turn_rows` row with `kind == COMPACT_TURN`, find the by-turn line keyed
`str(row["seq"])` in `payload["turns"]`. If it has BOTH

* a child keyed `f"{seq}/{WORKER}"` (bill.py:101; `_fold` keys lines by `"/".join`,
  bill.py:378), and
* a child keyed `f"{seq}/{JARVIS}"` whose own children include
  `agent_usage.describe(agent_usage.COMPACTION)`,

then:

```
turn {seq}: the compaction is charged to the worker AND recorded as a Jarvis
compaction — one compaction, counted twice
```

Both conjuncts are required. The worker child alone is the pre-fix state of an order
whose `agent_calls` row was pruned past `CALL_LIMIT` (bill.py:1208) or never written,
and failing there would call a single count a double one. The Jarvis child alone is the
fixed, correct state.

This catches what the fold invariants cannot, by construction: it compares a row in
`wo_turns` against an item sourced from `agent_calls`, which are the two tables the
double count spans. REJECTED: asserting `payload["total"]` against a recomputed sum —
that is the same fold again, in different arithmetic.

SECOND, SMALLER REPAIR IN THE SAME FUNCTION, and a defect the fix would otherwise
create. `_check_calls`'s shortfall half runs only when EVERY recorded row has
`calls_cover` (bill.py:1497-1498). A compaction makes a full-sized API call and writes
no assistant message, so `_attach_calls` finds nothing in its window and never sets
`calls_cover` (bill.py:586-588) — meaning the shortfall check, the one that caught
wo-966987af's 184.6M-over-49.0M headline, is silently DISABLED on every order that has
ever been compacted. Both loops in `_check_calls` therefore skip `COMPACT_TURN` rows
(the `recorded` list at bill.py:1496 is built from the filtered rows), which both
restores the shortfall check and keeps the excess check from flagging a compact turn
that claims tokens with nothing found inside it.

### 4. Tests

`uv run pytest tests/test_bill.py tests/test_budget.py tests/test_fleetcost.py -q`
while working. `tests/test_bill.py` has NO compact-turn coverage today — that is why
this shipped.

1. `test_compaction_is_not_charged_to_the_worker` — a work order with two ordinary turns
   and one `compact` turn carrying an envelope, plus the matching `agent_calls`
   compaction row. `payload["total"]` equals the two worker turns plus ONE compaction;
   the `worker` actor line excludes it; the `jarvis` line includes it.
2. `test_compaction_keeps_its_turn_line` — the by-turn view still has a line for the
   compact seq, its only child is `jarvis`, and its tokens equal the compaction's.
3. `test_compaction_does_not_mask_a_transcript_gap` — compact turn plus an unrecorded
   worker turn, with `session.total` set BELOW `recorded`-as-of-today. The gap item is
   present, labelled "turns with no result JSON left", and sized from the worker turns
   alone.
4. `test_in_flight_compaction_does_not_mislabel_the_gap` — a running compact turn beside
   a settled unrecorded worker turn: label is "turns with no result JSON left" and
   `calls == 1`.
5. `test_only_compact_turns_reads_as_no_turns_on_record` — the `if not turn_rows` branch,
   via an order whose single row is a compact turn.
6. `test_double_counted_compaction_fails_reconcile` — construct the payload by hand with
   both children present: `checks["balanced"]` is false and the problem string names the
   seq. Plus the two negative cases (worker child alone, Jarvis child alone) staying
   balanced.
7. `test_shortfall_check_survives_a_compaction` — an order with a compact turn and a
   recorded worker turn claiming far more than its calls cover: the shortfall problem is
   still raised. Fails today.
8. `tests/test_budget.py` — `spent()` over the §2 fixture: `worker_usd` excludes the
   compaction, `jarvis_usd` includes it, `total_usd` is the honest figure. And a stale
   compact turn still sets `stale`, so the repair path still fires.

MUTATION CHECK, both required to fail:

* revert §1's filter to the raw `turn_rows`: cases 1, 2, 3, 4, 5 and 6 fail; §2's budget
  case still passes, which is what proves the two sites are independently covered.
* revert §2's `CASE` to the bare `SUM(cost_usd)`: only case 8 fails.

## What is NOT changed

* `worker_session._record_compaction` keeps writing BOTH rows. The `wo_turns` row is the
  TURN record — state, timing, `msg_id`, `context_peak`, the delta chain `_turn_usage`
  derives the next envelope against, and what `jarvis inspect` profiles. Deleting it
  would delete the OS's only record that the compaction ran.
* `fleetcost` — already correct (src/jarvis/fleetcost.py:67, :411-420).
* `compaction_payoff` — reads `agent_calls` and keeps doing so.
* `inspection` / `jarvis inspect` — time, not money. A compaction's wall clock belongs
  to the turn it ran as, and does not move.
* `agent_usage`, `central_store.wo_call_cost`, `Spend`'s shape, `ops._turn_rows`,
  `ops._turn_row`, `_turn_locator`, `_call_items`.
* Historical data: NO MIGRATION. Both fixes are at READ time, so every past order —
  sealed bills aside, which are frozen deliberately (`bill.build`, src/jarvis/bill.py:869)
  — re-prices correctly the next time it is asked for, and no stored number is
  back-filled (kn-96f47efb).
