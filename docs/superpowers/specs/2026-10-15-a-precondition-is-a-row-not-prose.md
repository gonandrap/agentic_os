# A precondition is a row, not prose

Work order wo-5a7f71b5 · GitHub issue 1089 · project jarvis_os

## 1. The problem

A work order can state a stop condition in its brief and be dispatched anyway, because
nothing in the dispatch path reads the brief.

wo-314221b5 was filed with `DO NOT START until SCHEDULED_ACTORS[validating] names the
held-round actor` as the FIRST LINE of its description, with `depends_on = []` and
`blocked_by = []`. The same work was already parked by the OS as OPEN central backlog item
bl-d5baa28b, titled `… HELD until the 5a predicate fix ships`, and nothing live ships that
fix (kn-5845939d).

`Daemon.dispatch_pending` (`src/jarvis/daemon.py:1499`) claims through
`ProjectStore.claim_next_pending` (`src/jarvis/project_store.py:2649`), whose WHERE clause is
the `CLAIM_RULES` fragment (`src/jarvis/project_store.py:1796`). Its five rules read
`status`, `hidden`, `retry_after`, `depends_on`, `feature_orders.max_parallel` and the
planner's `assumptions`. **None of them reads `title` or `description`, and none of them
reads the central backlog.** So the order was claimed, dispatched and run:

- 0.93 USD spent, the whole of it confirming the precondition the brief told it not to
  start without;
- abandoned with 0 commits;
- the empty submission escalated by the validation panel, spending a round;
- a user attention item held for 3.3h.

Root cause: **a precondition had no representation in state.** It existed in three places
that are all prose to the scheduler — the description, the backlog item's title, and a
knowledge entry — and in none that `CLAIM_RULES` can evaluate. The symptom fix (teach
something to grep the description) is refused in §2.4: a structural decision derived from
free text is kn-5d8a396a, and the existing precedent in this exact function is the
`gap_class` comment at `src/jarvis/ops.py:927-932`.

Expected: an order whose precondition is unmet stays `pending`, says what it waits on, and
costs no worker turn and no attention item.

## 2. The fix

Designed by Neo on question 1906. This section specs that design and does not reopen it.

### 2.1 One row per precondition, in the project store

New table in `SCHEMA` (`src/jarvis/project_store.py:695`), placed after the `assumptions`
table (`:939`) and with its index beside the others at `:1226`:

```sql
-- ONE EXPLICIT PRECONDITION on one work order: the thing it was told not to start
-- without, as a row the claim SQL can evaluate. Written ONLY by
-- `jarvis wo create --blocked-by <ref>` and cleared only by the daemon's re-derivation or
-- by `jarvis wo unblock` — NEVER derived from the brief (kn-5d8a396a, §2.4).
--
-- `ref` is deliberately free-form text, not a foreign key: the three things a hold points
-- at live in three different databases (`bl-` in the central store, `kn-` in the central
-- store, `io-` in this one) and one of them is a tracker issue or a URL. A column that
-- could only hold what SQLite can join would refuse exactly the holds that occur.
CREATE TABLE IF NOT EXISTS preconditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    ref TEXT NOT NULL,                      -- bl-… | kn-… | io-… | #123 | a URL | free text
    note TEXT NOT NULL DEFAULT '',          -- the user's own words for the wait
    status TEXT NOT NULL DEFAULT 'open',    -- open | cleared
    created_at REAL NOT NULL,
    cleared_at REAL,
    cleared_by TEXT NOT NULL DEFAULT '',    -- 'os' | 'user'; '' while open
    -- Idempotence for a repeated flag, and what makes "attach this hold" re-runnable.
    UNIQUE (wo_id, ref)
);
CREATE INDEX IF NOT EXISTS idx_preconditions_open ON preconditions(wo_id, status);
```

`ON DELETE CASCADE` rather than the bare `REFERENCES` `assumptions` uses: `db.connect`
enables `foreign_keys=ON` (`src/jarvis/db.py:13`) and `validation_rounds`
(`src/jarvis/project_store.py:961`) already takes this route, so `ops.delete_work_order`
cannot strand rows.

**Migration mechanism, stated because it is the thing most likely to be got wrong:** a
brand-new table needs NOTHING in `ADDED_COLUMNS` (`src/jarvis/project_store.py:1231`).
`__init__` runs `executescript(SCHEMA)` on every open (`:1901`), so `CREATE TABLE IF NOT
EXISTS` creates it on a live database on the next CLI invocation. `ADDED_COLUMNS` is only
for columns on tables that have already shipped — any LATER column on `preconditions` goes
there, with its reasoning, exactly as the `validation_rounds` comments at `:972-978` say.
No backfill: zero open preconditions is the true state of every existing row.

Two module constants beside `PROVISIONAL_VERDICTS` (`:1745`):
`PRECONDITION_STATUSES = ("open", "cleared")`, `PRECONDITION_CLEARED_BY = ("os", "user")`.

Four `ProjectStore` methods, beside `create_work_order` (`:2112`):
`add_precondition(wo_id, ref, note="")`, `open_preconditions(wo_id)`,
`clear_precondition(id_or_ref, *, by)`, `clear_preconditions(wo_id, refs, *, by)`. Each
`clear_*` writes an `add_event(wo_id, "precondition_cleared", {...})`; `add_precondition`
writes `precondition_added`.

### 2.2 The exclusion is a `_ClaimRule`, not a caller

New entry in `CLAIM_RULES`, inserted at **index 1** — directly after `selection` and
before `retry_after`:

```python
    # THE ORDER WAS TOLD NOT TO START, and said so structurally. In `CLAIM_RULES` and not
    # in `Daemon.dispatch_pending` for `plan_assumption`'s reason one level stronger: an
    # open precondition is a PROPERTY OF THE ROW (Neo 1906), unlike `claim_next_pending`'s
    # per-call `only`, which is a restriction on one caller. Here the row is passed over
    # inside the statement that picks the next one, so a younger unheld order is still
    # claimed on the same call and NOTHING IS WRITTEN — no status, no attempt, no flag.
    _ClaimRule(
        code="precondition",
        why="a precondition it was told not to start without is still open",
        sql="""NOT EXISTS (
                   SELECT 1 FROM preconditions p
                   WHERE p.wo_id = w.id AND p.status = 'open'
               )""",
        delayed=True,
        params=lambda: (),
    ),
```

**Why index 1 and not last.** The claim's UPDATE ANDs every rule, so position cannot change
whether a row is taken. It decides one thing only: which sentence
`ProjectStore.claim_refusal` (`:2693`) returns, because that function takes the FIRST
refusing rule. A precondition is the condition the order was CREATED with, and the other
four are circumstances it happens to be in; "blocked by wo-x" or "a dispatch is backing
off" said about an order that must not run at all is the misleading half of a true pair.
The cost is one probe of `idx_preconditions_open` per candidate row, which is why the index
exists.

**`delayed=True`, on `plan_assumption`'s precedent** (`:1863`). `claim_sql_selects`
(`:1881`) is the predicate behind `pending`'s entry in `ops.SCHEDULED_ACTORS`
(`src/jarvis/ops.py:17039`); `delayed=True` keeps `Daemon.dispatch_pending` as the row's
actor with a later turn, so the stuck sweep (`health.py:181`) and the supervisor never see
an unowned `pending` order and never buy a model session for a designed wait. `_pending_why`
(`src/jarvis/ops.py:17020`) renders the sentence verbatim after its prefix, which is
already how a user-held wait reads for `plan_assumption`.

`claim_refusal` and `why_not_claimable` (`:2712`) need no change at all: both are built
from `CLAIM_RULES`' own `sql` strings, so they get the sentence for free — the property
`tests/test_claim_rules.py::test_both_queries_are_built_from_the_same_rule_sql` pins.

### 2.3 The only writer is an explicit flag

`jarvis wo create` gains `--blocked-by <ref>`, repeatable (`action="append"`), beside
`--depends-on` at `src/jarvis/cli.py:800`. The handler at `:3581` passes the list through
unchanged, on the shape the four self-evolution flags use at `:3593-3596`.

`ops.create_work_order` (`src/jarvis/ops.py:893`) gains `blocked_by: list[str] | None =
None` and **passes it straight through**, in the explicit spirit of the `gap_class`
paragraph already in its docstring (`:927-932`): nothing here derives, guesses or
normalises it, and nothing reads `description` for it. The rows are written immediately
after `store.create_work_order` returns (`:951`) and before `record_issue_references`, in
the same `try` and with the store open — at creation, never as a follow-up write, for the
reason stated at `:962-964`: the daemon can claim a new row before a second statement
lands, and a hold that arrives after the claim is no hold.

Each ref is normalised by `preconditions.checked_ref` (§2.4) — whitespace stripped, a
refusal only for an empty string. No membership check and no existence check: an unresolved
`bl-` id is a finding about the OS's records, not an error (the `--ref` rule `jarvis io
create` already ships), and resolving it is §2.6's job.

`ProjectStore.create_work_order` is NOT given the parameter. It is the storage layer, it
takes no policy, and widening it would put the hold on the nine internal callers
(`daemon.py:1591`, `:1610`, `:1631`, `:4751`, `:5664`, `:9636`, `ops.py:1164`, `:10910`,
`project_store.py:3447`) that file the OS's own orders.

### 2.4 Prose holds are DETECTED, never attached

`src/jarvis/preconditions.py`, a new LEAF module importing nothing from `jarvis` (the
`timeline.py` and `gaps.py` tier), owning the stop-condition phrase family and nothing else:

```python
STOP_PHRASES = ("do not start until", "do not start before",
                "do not dispatch until", "held until", "blocked until")
```

plus `states_hold(title: str, description: str) -> str | None`, which returns **the matched
sentence** (clipped, no ellipsis games) or None, and `checked_ref(raw) -> str`.

It lives there and not in `project_store`, `ops` or `invariants` because three unrelated
callers need the same answer — the creation refusal (`ops`), the detector
(`invariants`), and the tests — and a second copy of the phrase list is how the refusal and
the detector come to disagree about what a hold is. It is a leaf for `gaps.py`'s reason:
nothing it needs is above it.

Matching rules, all of them precision-first:

- case-insensitive, and the phrase must begin a LINE or follow `.`/`;`/`:`/start-of-text,
  optionally behind a bullet or `**` marker;
- there must be a non-empty remainder on the same line — the condition. `HELD until` with
  nothing after it names no hold.

**False-positive posture: precision over recall, deliberately,** because the predicate gates
a REFUSAL of a human's command (below) and a false positive makes a legitimate order
unfileable until the operator argues with a regex. Accepted false negatives, named so
nobody "improves" them in: `wait for X first`, `after Y ships`, `once Z lands`, `blocked
on`, `needs A`. Those read as sequencing inside a brief as often as they read as a stop
condition, and the cost of the miss is bounded — §2.5's detector fires on nothing, the
order runs, which is today's behaviour. The cost of the over-match is an operator who
cannot file.

**The refusal.** `ops.create_work_order` raises `OpsError` when
`preconditions.states_hold(title, description)` matches AND the order carries neither a
`depends_on` edge nor a `--blocked-by` row. The message names the matched sentence and both
wiring options verbatim:

```
cannot create this work order: its brief states a stop condition — "DO NOT START until
SCHEDULED_ACTORS[validating] names the held-round actor" — and nothing holds it back, so
jarvisd would claim and dispatch it (wo-314221b5 cost 0.93 USD this way). Wire the hold:
`--depends-on <wo-id>` when another work order in this project must COMPLETE first, or
`--blocked-by <bl-…|kn-…|io-…|#issue|text>` when it waits on something that is not a work
order. The brief may keep the sentence either way.
```

Raised BEFORE `store.create_work_order`, so a refused order leaves no row.

**This is a refusal and never an attachment.** The predicate must not write a precondition
row off its own match: the ref would be invented from prose, which is the whole defect, and
a hold the OS attached is a hold no reader can audit against a user's intent.

**It applies to `ops.create_work_order` only** — the three human-reachable creation paths
(`cli.py:3581`, `ui/app.py:2102`, `remedies.py:680`/`:684`). Plan children
(`ops.py:10910`) and the daemon's own filings go through the store directly and are NOT
refused: refusing at plan release would reject a plan a human has already approved, at the
last step, with no route back but a re-plan. A planner child that states a hold is caught
by §2.5 instead. That asymmetry is the point of having a detector at all.

### 2.5 Gap class `unwired-precondition`, with a real detector

New entry in `gaps.GAP_CLASSES` (`src/jarvis/gaps.py:74`) and in `SHIPPED_GAP_CLASSES`
(`:123`). **A catalog key there and nowhere else** — no module constant in `invariants`,
`ops` or `daemon` holds this slug; readers reach it through `gaps.get` / `gaps.by_invariant`.

```python
    "unwired-precondition": GapClass(
        id="unwired-precondition",
        headline="an order's brief states a stop condition that exists in no state the "
                 "scheduler reads, so the claim takes it and a worker pays to rediscover "
                 "the hold",
        symptom="a dispatched order whose first line says it must not start, abandoned "
                "with no commits",
        detector="INV-PRECONDITION-UNWIRED",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1089",
        since="",
    ),
```

It is the first entry in that registry to ship a non-empty `detector`; the other four carry
`""`, which `by_invariant()` (`:152`) excludes by design.

**The remedy is `REMEDY_NONE`, and that is an answer rather than a gap.** Which party is
obligated: the DETECTOR is obligated to fire, and nobody is obligated to act
mechanically. kn-a816ddf6 is the standing ruling — an armed detector raises its alarm even
with no armed remedy, so the detector is useful before any remedy is ever armed (quoted at
`docs/superpowers/specs/2026-10-14-a-killed-validation-panel-opens-an-investigation.md:281`).
The remaining obligation is the ORDER'S AUTHOR's: only they know whether the sentence names
a real hold (wire it) or stale text (delete it), and both candidate automatic remedies are
wrong — attaching a row invents a ref from prose (§2.4), and cancelling the order destroys
work on the strength of a regex. `drop_hold` and `raise_attention` exist in
`remedies.SHIPPED_REMEDIES` and neither applies: there is no hold to drop and nothing to
raise beyond what the notification already says.

**kn-b6700253 does not overturn this.** It rules that when a condition is detected AND it
brakes the fleet AND NO USER DECISION IS INVOLVED, the design is unfinished until
something is dispatched. Here a user decision IS involved for an unwired prose hold — only
the order's author knows whether the sentence names a real hold to wire or stale text to
delete — and the one case the OS CAN derive, a `bl-` ref, is cleared by the OS itself in
§2.6, which is the named actor. So `REMEDY_NONE` stands, under kn-a816ddf6's standing
ruling above.

**The detector.** `INV_PRECONDITION_UNWIRED = "INV-PRECONDITION-UNWIRED"` and
`check_precondition_is_wired(store)` in `src/jarvis/invariants.py`, registered in
`INVARIANTS` (`:5322`) and NOT in `SLOW_INVARIANTS` (`:5403`): its population is open
orders, not settled history, and it shells out to nothing.

Predicate, per `store.list_work_orders(statuses=OPEN_STATUSES, include_hidden=True)` row:
`preconditions.states_hold(title, description)` matches, AND `store.dependencies(wo)` is
empty, AND `store.open_preconditions(id)` is empty. That is exactly the set the creation
refusal cannot reach — orders that predate it, planner children, and anything the OS filed
itself.

`Violation(invariant=INV_PRECONDITION_UNWIRED, wo_id=…, detail=…, repaired=False,
owed="", context={"matched": <sentence>})`.

**`owed` stays EMPTY, and that is a ruling, not an omission.** `Daemon.check_invariants`
(`src/jarvis/daemon.py:5748`) computes `routed = bool(v.owed and v.wo_id)`; a routed
violation raises the work order's flag with a reason DERIVED from `true_blockers` and
SUPPRESSES the notification (`:5766`). With no `true_blockers` branch, a non-empty `owed`
here would route to a derivation that returns nothing — no flag AND no notification, a
silent check. The alternative, a `true_blockers` branch, puts the §2.4 regex on the hot path:
`true_blockers` (`invariants.py:1030`) runs per open order per tick on several surfaces.
So: empty `owed`, `wo_id` set, which gives one `kind="invariant"` timeline event on the
order plus one notification per standing violation (`:5761-5779`, deduped by
`open_violation_report`), reaching `jarvis inbox`, Telegram and `jarvis doctor`. No attention
flag, so INV-ATTENTION-REASON has nothing to relabel.

Ordering in `INVARIANTS`: order-free — a pure read that repairs nothing, raises no flag and
writes no status, so nothing else in the tuple reads its output. Place it after
`check_attention_has_reason` with that sentence as its comment, matching the convention of
every neighbour.

### 2.6 Re-derivation, so this hold can never become `stale-hold`

A hold written once and never re-derived is the `stale-hold` class (`gaps.py:75`). Two
clearing parties, split by what the OS can actually derive.

**The OS, every reconcile tick.** `Daemon.clear_settled_preconditions(project, store)`,
called from `tick()` **immediately before `self.hold_red_release` at
`src/jarvis/daemon.py:1268`**, gated on `reconcile`. That position for the reason the
comment there already gives about `sync_issues`: `dispatch_pending` is the next line
(`:1269`), so a hold cleared on this tick is claimed on this tick rather than one poll
interval later.

For each open precondition row whose `ref` starts `bl-`: read `self.central.get_backlog(ref)`
(beside `list_backlog`, `src/jarvis/central_store.py:761`; the `backlog.status` vocabulary
is `open | promoted | done | dropped`, `:140`). Clear the row with `by="os"` when the item
exists and its status is NOT `open`. One indexed read per open row, and zero work for a
project with none.

**A `bl-` ref that resolves to NOTHING does not clear.** A missing row means the ref names
nothing the OS can reason about — a typo, or a deleted item — and clearing on absence would
release an order whose hold was never really satisfied. It stays open and is cleared by a
human, like every other underivable ref.

**Non-`bl-` refs are never cleared by the OS.** A `kn-` entry, an `io-` id, an issue number
and free text have no state the OS can read as "satisfied".

**The human: `jarvis wo unblock`,** extended rather than duplicated — the command already
means "cut what is holding this order back", and a second command for the second kind of
hold is two things for the user to remember and two to forget.
`ops.unblock_work_order` (`src/jarvis/ops.py:8402`) and its parser (`cli.py:848`):

- **default** cuts dead dependency edges (`invariants.dead_dependencies`, unchanged) AND
  clears every open precondition row the daemon can never clear — every non-`bl-` ref, and
  a `bl-` ref resolving to no central row. That is the same criterion the dependency default
  already uses: these are the holds that strand the order forever.
- **`--all`** additionally clears `bl-` rows whose backlog item is still `open` — the user
  saying "run it anyway, without the thing it was told to wait for", which is exactly what
  `--all` already means for a live dependency edge.
- the two refusals widen: `is not blocked by anything` must now count rows as well as edges,
  and `waiting on work that is still live` must list the live `bl-` refs and point at
  `--all`.
- the return payload gains `preconditions_cleared: [ref, …]`, beside `dropped` and
  `still_blocked_by`.
- `clear_attention` (`:8431`) stays keyed on dependencies alone: a precondition never raised
  a flag (§2.7), so there is nothing to lower.

Each clear writes `precondition_cleared` with `{"ref": …, "by": "user"|"os", "note": …}`;
`src/jarvis/timeline.py:228` `_describe` gains an arm for it and for `precondition_added`.
Neither goes in `DEBUG_KINDS` (`:42`): one row per hold per order lifetime, and what an
order waited on is the story, not the plumbing.

### 2.7 Surfacing — and it raises no attention

**A designed wait is not an attention item.** `true_blockers` gets NO branch, so
INV-ATTENTION-MISSING (`invariants.py:2553`) stays quiet without an exception — the same
rule that already keeps an unfinished dependency, a `max_parallel` slot and a plan hold
flagless (`status_label`'s own comments, `invariants.py:1908-1918`), and the rule
kn-4f88d1b7 states about enumerating designed waits where the state is readable. The state
IS readable here: that is what §2.1 bought.

Five readers, each named because a sixth deriving its own answer is how two surfaces come to
disagree:

1. **`invariants.status_label`** (`:1790`), in its `pending` tail. A new branch ABOVE
   `store.plan_hold` (`:1895`), making four ranked answers instead of three: of them it is
   the only one that may never clear without a person, and it is the condition the order was
   created with. Renders
   `pending — held until bl-d5baa28b clears (`jarvis wo unblock wo-314221b5`)`, with the note
   appended when there is one. **This one line is most of the surfacing work**: `jarvis wo
   list` (`cli.py:3672`), the pending line in `jarvis status` via `ops.os_status`'s
   `status_label` (`ops.py:718`), the dashboard listings and the work order's own page
   (`ui/templates/work_order.html:56-61`) all render through it.
2. **`jarvis wo show`** — a `"preconditions"` key beside `"blocked_by"` at `cli.py:3687`,
   always present and empty when there are none (`validation_rounds`' never-always rule,
   `:3709`), plus a `_readable_preconditions` in the chain at `:3772`.
3. **`ops.open_preconditions(store, wo)`**, a new sibling of `ops.blocked_by` (`:984`) with
   the same no-query-for-the-common-case shape, and a `"preconditions"` key beside
   `"blocked_by"` in `os_status` (`:710`) under the comment there: derived once, with the
   store open, so every surface gets one answer.
4. **`health.py`'s `dependency` blocker id** (`:72`, arm at `:210`): that arm becomes
   `ops.blocked_by(pstore, row) or ops.open_preconditions(pstore, row)`. This is the
   spend-critical reader — it is what makes the fleet-health sweep report the order as
   BLOCKED rather than stuck, so no sweep session is bought. No new id in `BLOCKERS`:
   `health_reassert_blockers` is a catalog setting, and a new member silently changes what
   every existing project counts.
5. **`ops.waiting_on`** (`:1428`) gains a `{"what": "precondition", "stalled": False}` arm
   before its status fall-through, so `jarvis wo why` and `jarvis wo resume-auto` name the
   hold instead of guessing at a permission prompt.

`ops.blocked_by` itself is NOT widened to return precondition rows, and this is the one
place I have departed from the issue's literal wording. Its entries are rendered as work
order links — `ui/templates/project.html:148-152` emits `/wo/<project>/{{ b.id }}` and
prints `b.status` — so a precondition entry in that list is a dead link and an empty
status on two pages. The `dependency` blocker id is reused anyway, at `health.py:210`, which
is what the issue's requirement is actually about: no new blocker vocabulary.

### 2.8 Rejected alternatives

- **Parse the brief at dispatch** (the obvious fix, and the one a reviewer will propose).
  It makes dispatch depend on a regex over a worker's paragraph — kn-5d8a396a, and the
  precedent in this very function (`ops.py:927-932`). It also cannot say WHAT the hold is,
  so nothing can ever clear it, which manufactures a `stale-hold`.
- **Filter in `Daemon.dispatch_pending` instead of in `CLAIM_RULES`.** Neo 1906 ruled
  against it and `claim_next_pending`'s own docstring (`:2661`) states the test: `only` is a
  restriction on the caller, an open precondition is a property of the row. A caller-side
  filter must either release a claim it has already written, or spend the whole
  `max_concurrent` ceiling re-claiming the same held row inside one tick
  (`project_store.py:1856-1859`).
- **A `held` work-order status.** `project_store.py:281-285` is the standing rule:
  `waiting_pr_merge` earned a status because nothing derived it. This IS derived — from a row
  — so a status would be a second, forgettable copy of it, and every reader of
  `OPEN_STATUSES`, `SCHEDULED_ACTORS` and `stuck.assess` would owe it an answer.
- **Reuse `depends_on` by making the backlog item a work order.** It promotes a parked
  backlog item into dispatchable work to express "do not dispatch", which is backwards, and
  `depends_on` is checked against rows in ONE project database (`ops.py:977-979`).
- **A `blocked_by` column on `work_orders` holding a JSON list.** A row per hold is what
  lets one carry a note, a clearing party and a timestamp, and `UNIQUE (wo_id, ref)` is what
  makes attachment idempotent. The existing JSON `depends_on` is the thing a reader must
  query with `json_each` in the claim SQL (`:1823`); no reason to add a second.
- **Let the creation refusal attach the row itself.** Convenient and wrong: the ref would be
  invented from prose, and nobody could later audit the hold against the user's intent.

## 3. Test obligations

1. **`tests/test_claim_rules.py:143-147` must be updated, not merely extended.**
   `test_every_rule_carries_a_reason` pins the exact code list and the exact `delayed`
   tuple: they become `["selection", "precondition", "retry_after", "dependency",
   "max_parallel", "plan_assumption"]` and `[False, True, True, True, True, True]`. Same for
   `tests/test_scheduled_actor.py:329`'s rule lookup if it names positions.
2. **The claim leaves the row `pending` and writes NOTHING** (new, in
   `tests/test_claim_rules.py`): one order with an open precondition, one without.
   `claim_next_pending()` returns the second; the first is still `pending`, with
   `dispatch_attempts == 0`, `retry_after` NULL, `needs_attention` falsy, no new
   `wo_state_spans` row and no new `wo_events` row. Plus the starvation case in
   `test_the_oldest_held_row_does_not_starve_younger_ones`' parametrisation, with
   `precondition` as a third id.
3. **Both queries still read the same text** — `test_both_queries_are_built_from_the_same_rule_sql`
   covers the new rule automatically; assert `why_not_claimable` returns the new sentence
   and `claim_refusal(...).code == "precondition"` even with a live dependency ALSO present
   (the index-1 ranking).
4. **`tests/test_gap_classes.py:17`** — `tuple(GAP_CLASSES) == SHIPPED_GAP_CLASSES` closes
   the registry with the fifth member; add that `unwired-precondition` is the only entry
   with a non-empty `detector`, and that `gaps.by_invariant()["INV-PRECONDITION-UNWIRED"]`
   resolves to it.
5. **`--blocked-by` writes rows and nothing else does** — a new
   `tests/test_preconditions.py`: the flag round-trips repeatably through
   `ops.create_work_order`; a brief containing every `STOP_PHRASES` member with the flag
   present creates normally; a work order created through `ProjectStore.create_work_order`
   with the same brief gets NO row (nothing infers).
6. **The refusal**: `OpsError` for each phrase with no edge and no row, the message
   containing the matched sentence, `--depends-on` and `--blocked-by`; no row left behind;
   accepted with either wiring; and the named false negatives (`wait for X first`, `after Y
   ships`, `once Z lands`) create normally.
7. **Re-derivation**: a `bl-` precondition clears on the reconcile tick when the backlog
   item goes `promoted`/`done`/`dropped`, with a `precondition_cleared` event and
   `cleared_by == "os"`; it does NOT clear while the item is `open`, nor when the item does
   not exist; a `kn-` ref never clears by itself.
8. **`jarvis wo unblock`**: default clears the underivable rows and refuses on a live `bl-`
   one naming `--all`; `--all` clears it; `preconditions_cleared` is in the payload; the
   dependency-only behaviour is unchanged.
9. **The detector**: INV-PRECONDITION-UNWIRED fires for a live order with a prose hold and
   no wiring; is silent once either wiring exists; is silent for a settled order; raises NO
   attention flag and DOES leave a notification (the `routed` arithmetic of §2.5).
10. **No attention and an actor**: `ops.scheduled_actor` on an order with an open
    precondition returns `Daemon.dispatch_pending` with the sentence in `why`, and
    `invariants.true_blockers` returns nothing for it.

## 4. Out of scope

- **Disposing of wo-314221b5's own escalated round 1.** It is the user's decision on an
  order that has already delivered, and nothing in this spec reaches a round.
- Teaching the central backlog to hold a precondition of its own. bl-d5baa28b's `HELD
  until …` title is the same defect one level up; it costs no worker turn, so it buys no
  release.
- Any widening of `STOP_PHRASES` beyond the five. A sixth phrase is a one-line diff behind
  the §2.4 posture and the tests in §3.6; adding it speculatively is how precision is lost.
