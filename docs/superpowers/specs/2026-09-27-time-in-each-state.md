# Time in each state

Work order wo-f7b00f9f. Decision taken by Neo for the user, question 817: Option A — one
durable status-transition table for both order kinds, one pure reader in `ops`.

## The problem

**How long an order has been in its current state, and how long it spent in each state
before that, is not derivable from any surface the OS has.** Three orders sat 7–13h in
`waiting_pr_merge` / `needs_review` with nothing progressing (wo-8736a5c5, wo-00bd1096,
wo-4301a7a6) and the only reading available was raw epoch `work_orders.updated_at`.

What exists and why each one is not the answer:

- `work_orders.updated_at` (`project_store.py:581`) is overwritten by EVERY
  `update_work_order` — a cost stamp, a `pr_state` poll, an attention flag — so it does not
  date the status it sits beside.
- `ops._diagnose_clock` (`src/jarvis/ops.py:1476-1498`) reads
  `store.events_of_kind(wo_id, "status")` and keeps **only the last one**: `last_status` is
  one moment, and `jarvis wo why` prints it as "3.1h ago". No totals, no re-entries, no
  per-status split, and nothing for a feature order.
- `holds.held` (`src/jarvis/holds.py:198`) partitions time by CAUSE of a hold, not by
  status. Its causes and the statuses overlap without matching: `waiting_pr_merge` — the
  status in the evidence — holds nothing in `_OPEN`/`_CLOSE` at all, so the 13h is invisible
  there too.
- The dashboard's work-order page (`src/jarvis/ui/templates/work_order.html`) has three
  tabs — Conversation, Spend, Timeline — and the Timeline tab prints `fmt_age(e.ts) ago`
  per event. A reader can subtract two of those by hand; that is the current mechanism.

Root cause, and it is a storage gap rather than a rendering one: **`ProjectStore.set_status`
(`project_store.py:2195-2198`) records the status it moved TO and never the one it moved
FROM, and `ProjectStore.set_feature_status` (`project_store.py:2541-2559`) records nothing
at all.**

```python
def set_status(self, wo_id: str, status: str, **extra: Any) -> None:
    assert status in WO_STATUSES, status
    self.update_work_order(wo_id, status=status, **extra)
    self.add_event(wo_id, "status", {"status": status})
```

For a work order the event trail is *just* sufficient — `kind='status'` rows plus
`created_at` as the start of the initial `pending` span — so spans are reconstructible but
nobody reconstructs them. For a feature order it is not sufficient in principle:
`wo_events.wo_id` is `REFERENCES work_orders(id)` (`project_store.py:637`) and
`db.connect` sets `foreign_keys=ON` (`db.py:13`), so a feature order **cannot** have an
event row, and `carrier_for_feature` (`project_store.py:3626`) says so in as many words.

Two further gaps this work order has to close because its own output would otherwise carry
them:

1. **One writer bypasses the chokepoint.** `ops.park_unlanded` (`src/jarvis/ops.py:3645-3647`)
   calls `store.update_work_order(wo["id"], status="needs_review")` directly. It exists to
   avoid a duplicate write, not to avoid the status machine: the branch above it is the
   "same episode, already parked" case, `Daemon.settle_work_order` re-derives an order's
   ending from the latest turn on EVERY tick, and an unconditional `set_status` there would
   add a `status` event per tick (the docstring at `ops.py:3628-3636` argues exactly this).
   So the bypass is a **no-change re-assertion**, and it is the only status write in the
   package that does not go through `set_status` — 36 other call sites do.
2. **`timeline.STATUS_LABEL` (`src/jarvis/timeline.py:81-94`) has no entry for
   `waiting_pr_merge` or `budget_exhausted`** — the two statuses in the evidence. Nine
   statuses of eleven are named; those two fall through `STATUS_LABEL.get(status, status)`
   at `timeline.py:213` and print as raw tokens. kn: a new event or field is invisible until
   its renderer names it; here an OLD one is half-named, and the new view would inherit it.

## The fix

One table written from the two chokepoints, one backfill, one pure reader in `ops`, and
three renderers that compute nothing.

### 1. `wo_state_spans` — the table

In `project_store.SCHEMA`, after the `health_reviews` block (`project_store.py:695-705`)
and before `scheduled_jobs`: it is the same shape of record — a per-project ledger keyed on
a polymorphic subject — and the comment there already argues the no-foreign-key case this
one needs.

```sql
-- EVERY STATUS MOVE OF EVERY ORDER IN THIS PROJECT, work orders and feature orders in one
-- table. One table and not two because the two loops record identical facts and a reader
-- of both (`ops.state_durations`, and the fleet-health checker that consumes it) would
-- otherwise be two readers that can disagree about what a span is.
--
-- NO FOREIGN KEY ON `order_id`, and that is a consequence of the polymorphism rather than
-- laxity: the id is either a `work_orders` or a `feature_orders` row and SQLite has no
-- way to reference one of two tables from one column. The alternative shape — the nullable
-- `wo_id`/`fo_id` pair with a CHECK that `validation_rounds` uses (project_store.py:784)
-- — buys ON DELETE CASCADE and costs every reader a two-column predicate; it is rejected
-- below. `delete_work_order` deletes these rows itself, exactly as it already does for
-- `health_reviews`.
--
-- `from_status` is '' for an order's FIRST row only (nothing preceded it).
-- `trigger` is '' whenever no caller named one, which is nearly always; the reader
-- derives a cause from the neighbouring timeline event instead — see §4.
-- `approximate` marks a row the BACKFILL inferred rather than observed (§3): a feature
-- order has no event trail, so its history is one coarse span and must say so.
CREATE TABLE IF NOT EXISTS wo_state_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    order_kind TEXT NOT NULL,           -- 'wo' | 'fo'
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL,
    ts REAL NOT NULL,
    trigger TEXT NOT NULL DEFAULT '',
    approximate INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_state_spans_order
    ON wo_state_spans(order_id, ts);
```

One index and it is the only read pattern: every consumer asks for one order's rows oldest
first. `(order_kind, ts)` is NOT indexed — no surface asks "every transition in this
project" and an unused index is a write cost on the hot path.

New table, so `CREATE TABLE IF NOT EXISTS` at `project_store.py:1483` is the whole upgrade
for the DDL; `ADDED_COLUMNS` is not involved. `tests/test_schema_upgrade.py` proves that.

### 2. The two writers, and the no-change guard

`ProjectStore.set_status` (`project_store.py:2195`) and `set_feature_status`
(`project_store.py:2541`) each gain `trigger: str = ""` (keyword) and each records a row.
Both read the current status first, in the same connection, and **a move to the status the
order is already in writes nothing** — no span row, and no `status` event either:

```python
def set_status(self, wo_id: str, status: str, *, trigger: str = "",
               **extra: Any) -> None:
    assert status in WO_STATUSES, status
    was = str((self.get_work_order(wo_id) or {}).get("status") or "")
    self.update_work_order(wo_id, status=status, **extra)
    if was == status:
        return          # a re-assertion is not a transition; see ops.park_unlanded
    self._record_span(wo_id, "wo", was, status, trigger)
    self.add_event(wo_id, "status", {"status": status})
```

Three consequences, all wanted:

- **The bypass at `ops.py:3645-3647` is closed by becoming unnecessary.** Replace it with
  `store.set_status(wo["id"], "needs_review", trigger="unlanded_repark")` and delete the
  `update_work_order` call. The duplicate `status` event `park_unlanded` was dodging is now
  suppressed by the store, for every caller, rather than by one caller knowing to dodge it.
  Its docstring paragraph at `ops.py:3638-3643` stays true and should say the store now
  enforces it.
- `extra` still applies on the no-change path — `set_status(..., pr_url=…)` must keep
  writing the column.
- Duplicate `status` events stop being written fleet-wide. Nothing reads them by count:
  `_diagnose_clock` takes the last, `timeline._describe` renders each (one fewer repeated
  "Needs your review" line), `holds` does not read the kind, and no test in `tests/`
  asserts on `kind='status'` at all.

`_record_span(order_id, kind, from_status, to_status, trigger)` is one private insert
beside `add_event` (`project_store.py:282`), stamping `db.now()`.

**No other call site changes.** 36 `set_status` and 17 `set_feature_status` callers pass no
trigger and do not need to: §4 derives the cause at read time by a rule that gives a
stored-trigger row and an un-triggered row the same treatment, so annotating call sites is
an optional later improvement and never a correctness requirement.

### 3. The backfill, in `_migrate`

Two helpers called from `ProjectStore._migrate` (`project_store.py:1486-1498`) after
`_backfill_pending_at`, following `_backfill_alarms`' idempotence discipline
(`project_store.py:1548-1586`): a cheap count fast-path, then a SET rebuilt from the table
as the real guard, because `_migrate` runs in `__init__` — every CLI invocation and every
reconcile of every project.

**`_backfill_wo_spans` — EXACT.** For each work order with no `wo_state_spans` row: read
`wo_events` `kind='status'` ordered by `ts`, and write

- `('', 'pending', created_at, approximate=0)` — the row the events cannot carry, because
  `create_work_order` inserts `status='pending'` without a status event;
- then one row per status event, `from_status` = the previous event's status (or `pending`),
  `to_status` = `payload["status"]`, `ts` = the event's `ts`;
- **dropping any event whose status equals its predecessor's** — that is the duplicate §2
  now prevents, and a zero-length span in the history would inflate the entry count.

`approximate=0` on all of them: the timestamps are the OS's own write moments, not
estimates.

**`_backfill_fo_spans` — APPROXIMATE, one span.** A feature order has no timeline (§ the
problem). For each `feature_orders` row with no span row, write exactly one:
`('', <current status>, created_at, approximate=1)`. Its `updated_at` is not used as a
boundary — a single span from creation to now, labelled as inferred, is the only honest
reading; claiming `created_at → updated_at` in the current status would date the status
from the last unrelated column write.

The flag rides out to both surfaces (§6, §7). Neo's ruling: a labelled coarse span beats a
missing one.

`trigger` is '' on every backfilled row. Nothing is derived at write time — §4 does it at
read time, identically for live and historical rows, which is what makes them
indistinguishable to a reader.

### 4. `ops.state_durations` — the only computation

```python
def state_durations(store: ProjectStore, *, wo_id: str = "", fo_id: str = "",
                    now: float | None = None) -> StateDurations:
```

Discriminated by keyword exactly as `ops.validation_rounds(store, wo_id=…)` /
`ops.validation_detail(store, fo_id=…)` already are — one of the two, `OpsError` on both or
neither. `now` is a parameter and defaults to `time.time()` inside, the rule `holds.held`
follows (`holds.py:198-209`): every figure here is a PRESENT-TENSE claim computed at read
time from immutable rows (kn-96f47efb), so it must be both un-cacheable and injectable by a
test.

Two dataclasses in `ops`, mirroring `holds.Hold`'s shape — frozen, `now` never baked in,
`as_dict(now)` for the surfaces:

```python
@dataclass(frozen=True)
class Span:
    status: str
    entered: float
    left: float | None          # None = the order is in this status now
    trigger: str                # '' when the record names no cause
    approximate: bool
    #  seconds(now) -> float    # max(0.0, (left or now) - entered)

@dataclass(frozen=True)
class StateDurations:
    order_id: str
    order_kind: str             # 'wo' | 'fo'
    spans: tuple[Span, ...]     # oldest first
    current_status: str         # '' for a settled order
    current_status_since: float | None
    last_activity_ts: float | None
    last_activity_kind: str     # the wo_events kind / table that supplied it, '' if none
    approximate: bool           # any span is
    notes: tuple[str, ...]
```

`as_dict(now)` — and this IS the `--json` shape, one document for CLI and UI:

```python
{
  "order_id": "wo-8736a5c5", "order_kind": "wo", "now": 1758900000.0,
  "approximate": false,
  "lifetime_seconds": 61200.0, "lifetime_human": "17.0h",
  "spans": [
    {"status": "pending", "label": "Queued", "entered": 1758838800.0,
     "left": 1758838860.0, "open": false, "seconds": 60.0, "seconds_human": "60s",
     "share": 0.001, "trigger": "created", "approximate": false},
    …
  ],
  "totals": [
    {"status": "waiting_pr_merge", "label": "Waiting for its pull request to merge",
     "seconds": 47160.0, "seconds_human": "13.1h", "entries": 1, "share": 0.771},
    …
  ],
  "current_status": "waiting_pr_merge",
  "current_status_since": 1758852840.0,
  "current_status_age": 47160.0, "current_status_age_human": "13.1h",
  "last_activity_ts": 1758852840.0, "last_activity_kind": "turn_ended",
  "last_activity_age": 47160.0, "last_activity_age_human": "13.1h",
  "notes": []
}
```

Units, stated because a consumer cannot guess them: every `*_ts`, `entered`, `left`,
`current_status_since` is an **epoch float in seconds** (`db.now()`); every `seconds`,
`*_age` and `lifetime_seconds` is a **float count of seconds**, rounded to 2; every `share`
is a **fraction of lifetime**, 0.0–1.0, rounded to 4. Every `*_human` comes from
`ops.ago_phrase` (`ops.py:1397`) and nowhere else — the renderer computes no duration, the
§6 rule this repo already enforces for `jarvis wo why`.

Construction rules:

1. Read `wo_state_spans` for the order, oldest first. Each row opens a span and closes the
   previous one at the same `ts`. Clamp with `max(0.0, …)` — a backwards clock must not
   produce a negative span (`holds.Hold.overlap`'s rule).
2. The last span is OPEN (`left=None`) unless the order's status is terminal
   (`TERMINAL_STATUSES` / `FO_TERMINAL_STATUSES`), in which case `left` is that row's own
   `ts` and there is no open span. **A settled order therefore has
   `current_status=""`, `current_status_age=None`.** `None` and never `0` — issue #227's
   rule, already written down at `ops.py:1427-1435`.
3. `lifetime_seconds` = (last `left`, or `now`) − first `entered`. **The spans sum to it by
   construction**, which is the property the tests pin.
4. `totals` is ordered by `WO_STATUSES` / `FO_STATUSES` — those tuples ARE the render order
   the dashboard uses (`project_store.py:50-52`) — and carries only statuses the order was
   actually in. `entries` counts spans of that status, so a re-entry accumulates seconds and
   increments entries.
5. `label` is `timeline.STATUS_LABEL` for a work order and
   `project_store.feature_status_label(kind, status)` (`project_store.py:454`) for a
   feature order, so an improvement order's rows read `analysing`, not `planning`.
   **`STATUS_LABEL` gains its two missing keys** — `"waiting_pr_merge": "Waiting for its
   pull request to merge"`, `"budget_exhausted": "Budget spent"` — or this view prints raw
   tokens for the two statuses it was built for.
6. `trigger`: the stored value when a caller passed one; otherwise derived — the kind of
   the newest `wo_events` row at `ts <= span.entered` within `TRIGGER_WINDOW = 120.0`
   seconds, excluding `kind='status'` and `health.observer_kinds()`. `''` when nothing
   qualifies, which is every feature-order row (no events exist) and any transition nothing
   was written about. One rule for live and backfilled rows, which is why no call site has
   to be annotated. Renderers print the raw kind and, when it is '', **"cause not
   recorded"** — never a guess and never blank.

#### `last_activity_ts`, precisely

The newest moment the RECORD says something happened to this order that was not the OS
looking at it. For a work order, the max over — all reads are indexed and per-order:

| the user's words | table.column |
|---|---|
| last worker turn | `wo_turns.started_at`, `wo_turns.ended_at` (`project_store.py:903-904`) |
| message | `wo_messages.ts`, `wo_messages.delivered_at` (`project_store.py:731,736`) |
| event | `wo_events.ts` (`project_store.py:638`) |
| validation round | `validation_rounds.ts` where `wo_id = ?` (`project_store.py:789`) |
| gate decision | `approvals.ts`, `approvals.decided_at` (`project_store.py:853,877`) |
| CI change | `wo_events.ts` for `landing_seen` / `pr_merged` / `pr_closed` / `pr_reopened` (`daemon.py:4855,4861`, `ops.py:4862`, `daemon.py:6014`) — there is no CI table; `work_orders.pr_state` is a cache with no timestamp and is NOT usable |

```
last_activity_ts = max(
    wo_events.ts        WHERE wo_id=? AND kind NOT IN (observer_kinds() + ('status',)),
    wo_turns.started_at, wo_turns.ended_at   WHERE wo_id=?,
    wo_messages.ts, wo_messages.delivered_at WHERE wo_id=?,
    validation_rounds.ts                     WHERE wo_id=?,
    approvals.ts, approvals.decided_at       WHERE wo_id=?
)   -- NULLs skipped; None when every one is NULL
```

Two rulings inside that:

- **Observer kinds are excluded, and this is a correctness rule rather than tidiness.**
  `health.observer_kinds()` (`src/jarvis/health.py:29-47`) is `(*ALARM_EVENT_KINDS,
  "attention")` — what the OS writes onto a unit *while watching it*. The consumer of this
  function is a checker that decides which orders are stuck; if being looked at moved
  `last_activity_ts`, an order the supervisor swept would read as progressing **because it
  was examined**, and the checker could never fire twice. This is the identical trap
  `observer_kinds` was written for, one surface along, so the same function is imported
  rather than a second list spelled out. `kind='status'` is excluded for the same reason
  from the other side: the status move is what §4 is already measuring, and counting it as
  activity would make every transition reset the idle clock.
- **All five tables are read even though every activity class in today's code also writes a
  `wo_events` row** (`turn_started`/`turn_ended` at `worker_session.py:585,777`,
  `message_queued`/`message_delivered` at `ops.py:1095`/`daemon.py:2936`,
  `validation_submitted` at `ops.py:3909`, `gate_requested`/`gate_decided` at
  `project_store.py:4455`/`gates.py:1437`, the CI kinds above). The union costs four cheap
  indexed `MAX()`s and cannot under-report; the events-only read is one future writer away
  from calling a busy order idle. `last_activity_kind` names which source won, so the
  redundancy is visible rather than silent.

For a **feature order**: the max over the same expression for **every work order in the
family** — `feature_children(fo_id)` plus the planner (`plan_wo_id`) plus the manager
(`manager_work_order`) — plus `validation_rounds.ts WHERE fo_id = ?`. NOT
`carrier_for_feature` (`project_store.py:3626`), and the distinction is the point: the
carrier ladder answers "where is a record ABOUT this feature written", and picking one
carrier would call a feature idle while three of its other children were running. A feature
progresses when anything in it does. A feature with no children and no planner has
`last_activity_ts = None` and a note saying so — absent, not zero.

`notes` carries the sentences an absence needs, as module constants beside
`ops.NO_TURN_NOTE` (`ops.py:1430`): the feature-order approximation, "nothing has happened
to this order on the record, so there is no last activity to date — absent, not zero", and
the settled-order "this order has settled, so it has no current status age".

### 5. Who else reads it

Nobody, yet. The companion fleet-health order consumes `state_durations` and is explicitly
out of scope here (§ Not covered). This spec adds no threshold, no alarm and no attention
flag: a number and a checker are two changes, and shipping the number alone is what lets
the checker be argued about separately.

### 6. CLI

**`jarvis wo show`** (`src/jarvis/cli.py:2606-2678`). Inside the `store` block, beside
`"alarms": store.alarms_of(...)` (`cli.py:2664`), add — always present, the stated rule of
that dict:

```python
"time_in_state": ops.state_durations(store, wo_id=args.wo_id).as_dict(),
```

`--json` prints it verbatim. The human path gains `_readable_time_in_state` in the chain at
`cli.py:2675-2677`, written to `_readable_rounds`' idiom (`cli.py:66-82`): HUMAN ONLY,
collapse to lines, `--json` untouched.

```
time in state:
  🔀 waiting_pr_merge   13.1h   1 entry    77%   ← now, 13.1h, nothing since 13.1h ago
  👀 needs_review        2.9h   2 entries   17%
  🟢 running             1.0h   1 entry      6%
```

Icons from `cli.STATUS_ICON` (`cli.py:317-322`), which already has all eleven statuses.
Every number is read off the payload; the renderer formats nothing.

**`jarvis fo show`** (`src/jarvis/cli.py:2858-2915`). `ops.show_feature_order` gains the
same `time_in_state` key (so `--json` has it), and the human branch prints the same block
after the `alarms:` line (`cli.py:2899-2900`). When `approximate` is true the block is
preceded, once, by:

```
  (approximate — a feature order keeps no event trail, so this is one coarse span
   from its creation)
```

### 7. UI

**Work-order page.** Route `work_order` (`src/jarvis/ui/app.py:1060-1158`): inside the
`store` block, `states = ops.state_durations(store, wo_id=wo_id).as_dict()`, passed to the
template as `states=states`. A **fourth tab** in the tab strip at
`work_order.html:416-429`, after Timeline, `id="tab-states"`, labelled `Time in state`
with `<span class="n">{{ states.current_status_age_human }}</span>`; the panel is a
`<section class="tabpanel" id="tab-states" role="tabpanel">` beside the existing three
(`work_order.html:431,486,497`). A tab and not a header block: the ask is diagnostic, and
`work_order.html:413-415` states the rule that only things asking something of the reader
live above the tabs.

**Feature-order page.** Route `feature_order` (`src/jarvis/ui/app.py:983-1009`) already
opens a `ProjectStore` for `validation_detail` (`app.py:999-1003`) — take the reading in
that same block and pass `states=…`. Render the same fragment in
`feature_order.html`, after the validation section.

**The fragment: `src/jarvis/ui/templates/_states.html`**, included by both pages, matching
`_debug_anatomy.html`'s idiom (`.panel` / `.row` / `.kv` / `.mono` / `.sub` / `.grow`) and
reusing `_debug.html`'s stacked-bar trick (`_debug.html:28-40`) — **no charting
dependency, no JavaScript, one flex row of divs whose widths are `share * 100%`**:

- one `.panel` of `.row`s, one per entry in `totals`: icon + label, `fmt_dur(seconds)`,
  `entries`, and the bar;
- the bar's colours come from `status_meta[s].tone` / `fo_status_meta[s].tone`
  (`ui/app.py:49`, `:76`) mapped to the dashboard's existing `--ok/--warn/--bad/--ink-3`
  variables, via `.get(s, …)` and never `[s]` — a twelfth status must render grey, not a
  500 on the page someone opened to find out why nothing moved (`_debug.html:33-34`);
- the **Gantt** is a second `.panel`: one `.row` per span, oldest first, each an indented
  bar positioned by `(entered − first_entered) / lifetime` and sized by `share`, with the
  transition's trigger (or "cause not recorded") in a `.sub` beside it, plus
  `fmt_ts(entered)`;
- the current span's row carries two figures and they are labelled differently —
  `in this status 13.1h · nothing on the record for 13.1h` — because equal numbers are the
  common case and a reader shown one number twice assumes a bug;
- `approximate` renders `{{ ui.note(…) }}`-style, in the CLI's words from §6 verbatim;
- `fmt_dur`, `fmt_ts`, `fmt_age` are already template globals (`ui/app.py:772`).

Degradation: the reading is a plain indexed read that cannot fail on data, but wrap it as
the debug page's blocks do (`_debug.html:13-19`) if it is taken outside the `try`. A 500
here costs an inbox item and an `INV-UI-HEALTHY` fleet alarm.

## Rejected alternatives

- **Derive spans from `wo_events` at read time; add no table.** Works for work orders and
  is impossible for feature orders — `wo_events.wo_id` is a real FK into `work_orders` with
  `foreign_keys=ON`, so a feature can never have a row. Half the ask would have no data
  source, and the fo half would need a second mechanism anyway. Neo's question 817 settled
  this; the exact-backfill in §3 is where that derivation still gets used, once.
- **The `wo_id`/`fo_id` nullable pair + CHECK, as `validation_rounds` has
  (`project_store.py:784-820`).** It buys `ON DELETE CASCADE`. It costs every read and every
  index a two-column predicate, and its own comment at `:822-825` documents the trap it
  brings (UNIQUE over a NULL column enforces nothing). `health_reviews`
  (`project_store.py:687-689`) already chose the single `subject_id` for this exact reason,
  with `delete_work_order` cleaning up; follow the nearer precedent.
- **Store the durations, or a `status_since` column.** A derived total written down goes
  stale the moment the order moves and needs a writer at every mutation — kn-96f47efb, and
  `project_store.py:1099`'s own rule: "`waiting_pr_merge` earned a status because nothing
  derived it; this does not". The rows are immutable, the arithmetic is at read time.
- **Extend `holds.held` instead of a new function.** `holds` answers "was the order allowed
  to work", clips to the gaps between turns and merges overlapping causes
  (`holds.py:28-46`). Status spans must NOT be clipped or merged — they tile the lifetime
  and must sum to it — so the two would fight inside one walk. `waiting_pr_merge`, the
  status in the evidence, is not a hold cause at all.
- **Pass `trigger=` at all 36 `set_status` call sites.** 36 edits, 36 chances to write a
  different word for one cause, and no reader is worse off without them: §4's read-time
  derivation covers a stored and an absent trigger identically. One site is touched —
  `ops.py:3645` — and only because it is the bypass.
- **Compute the block in each renderer, from the rows.** Three derivations of one number
  is what `jarvis wo why` was built to avoid (`ops._print_diagnosis`'s docstring,
  `cli.py:2741-2753`) and what would make the health checker a fourth. One function.
- **Count every `wo_events` row as activity, observers included.** Simpler and wrong: the
  supervisor's own sweep writes onto the order, so a stuck order would look alive because
  it had been examined. `health.observer_kinds()` exists for the identical failure in the
  fingerprint.
- **Use `work_orders.updated_at` as the current-status timestamp.** It is what the user was
  forced to read and it is not that fact: every column write moves it.

## Tests required

New file `tests/test_time_in_state.py`, built against a real `ProjectStore` in the style of
`tests/test_active_time.py:49-70` (`class Record`) — timestamps back-dated with SQL after
the store stamps `db.now()`, because there is no back-dating API. `now` is passed
explicitly to `state_durations` throughout; nothing sleeps.

1. **Spans sum to the lifetime.** An order driven `pending → running → needs_review →
   waiting_pr_merge`: `sum(s["seconds"] for s in spans) == lifetime_seconds`, exactly, and
   `lifetime_seconds == now - created_at`. Repeat with a terminal status and assert
   `lifetime_seconds == terminal_ts - created_at` and that `now` moving does not change it.
2. **Re-entry accumulates.** `needs_review → running → needs_review`: one `totals` entry for
   `needs_review` with `entries == 2` and `seconds` the sum of both.
3. **Current-status age and last-activity age.** Order in `waiting_pr_merge` since T−13h
   with a `turn_ended` at T−13h and a `health_finding` at T−1m: `current_status_age ==
   13h`, `last_activity_age == 13h` — **the observer event must not move it**. Then write a
   `landing_seen` at T−10m and assert `last_activity_age == 10m` and
   `last_activity_kind == "landing_seen"`.
4. **A settled order has no current span.** `current_status == ""`,
   `current_status_since is None`, `current_status_age is None` — asserted against `None`
   and not falsiness, so a 0 regression fails.
5. **A brand-new order.** One open `pending` span, `entries == 1`,
   `last_activity_ts is None`, and the absent-not-zero note present.
6. **The no-change guard.** `set_status(wo, "needs_review")` twice: one span row, one
   `status` event, `entries == 1`. Then `ops.park_unlanded` twice over the same episode and
   assert the same — the regression test for the closed bypass.
7. **Feature order, exact going forward.** Drive an `fo` through `planning → executing →
   completed` via `set_feature_status`; spans tile, `approximate is False`.
8. **Feature-order activity is the family's.** A feature whose manager is idle and one child
   took a turn 5m ago: `last_activity_age == 5m`.
9. **Every status has a label.** Loop `WO_STATUSES` and assert `timeline.STATUS_LABEL[s]`
   exists and is not the raw token — the assertion that keeps `waiting_pr_merge` and
   `budget_exhausted` named. Same loop over `FO_STATUSES` through
   `feature_status_label("feature", s)` and `("improvement", s)`.

In `tests/test_schema_upgrade.py`: the existing fresh-vs-shipped comparison covers the new
table with no new test. Add the **backfill** cases there or beside them — build a database
holding a work order with `kind='status'` events and no spans, open it with today's code,
and assert (a) the spans reproduce the events exactly, (b) the first span is `pending` from
`created_at`, (c) a duplicate consecutive status event produces no zero-length span, (d) a
second open writes nothing further (idempotence), (e) a feature order gets exactly one span
with `approximate=1`.

CLI, in `tests/test_wo_why.py`'s neighbourhood or a new `test_time_in_state` case invoking
`cli.main`: `jarvis wo show --json` carries `time_in_state` with every key above, and the
human output contains the status word, a duration and an entry count.

UI, in `tests/test_ui_observability.py` (the file that already covers the debug page's
blocks): fetch `/wo/{project}/{wo_id}` and assert the tab and the per-status row render;
fetch `/fo/{project}/{fo_id}` and assert the approximate note is on the page. The point of
the second is the flag: an unlabelled coarse span is the defect Neo's ruling 3 names.

`uv run pytest tests/ evals/` before the PR.

## Not covered

- **The fleet-health checker.** It consumes `state_durations` and is a separate order. No
  threshold, alarm, attention flag or probe is added here — `probes.py` and
  `invariants.true_blockers` are untouched.
- **Backfilling feature-order history exactly.** Impossible: no event trail exists. The
  approximation is labelled, and it self-corrects — every transition from this release on is
  exact, so a live feature's record becomes exact as it moves.
- **The improvement-order page** (`ui/templates/improvement_order.html`). An `io` is a
  `feature_orders` row, so it gets spans and a `jarvis io show`-adjacent `--json` key for
  free through `set_feature_status`, but its page gains no fragment in this order.
- **Per-status ALARM thresholds, and any change to `holds`.** The active clock and the
  status clock stay two readings; nothing here subtracts held time from a span.
- **Backfilling `trigger` for historical rows at write time.** Derived at read time by §4's
  one rule, so a backfilled row reads exactly as a live one.
- **`ops._diagnose_clock`'s `last_status`.** It keeps answering its own narrower question
  for `jarvis wo why`; folding it into this reading is a follow-up, not this order.
