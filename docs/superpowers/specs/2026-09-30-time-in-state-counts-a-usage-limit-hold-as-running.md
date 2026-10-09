# Time in state counts a usage-limit hold as running

Issue 887. Spec for `ops.state_durations` and its two renderers.

## The problem

`ops.state_durations` (src/jarvis/ops.py:1880) and the `Span` / `StateDurations`
dataclasses behind it (src/jarvis/ops.py:1681-1767) are the ONE timing reader in the OS
that never consults `holds.held`. wo-0cb6dc6b entered `running` at 09-30 00:06; its turn
was refused on the fleet usage limit at 00:45 (`turn_paused`, `reset_at` 05:00) and
resumed at 05:30 (`turn_resumed`). No status event is written at a pause — deliberately,
kn-f1934cfc rule 2: NO COLUMN, NO STATUS, NO FLAG for a usage outage. So `state_spans`
holds one unbroken `running` span of ~5h24m for an order that was permitted to work ~45m,
and `jarvis wo show` / the work-order page print that 5h24m as time in `running` with a
lifetime share computed against it.

Every other clock already subtracts holds: `inspection` (the wall/active split the
`holds` module docstring states as law), `autopsy.anatomy_for`, `supervisor`
src/jarvis/supervisor.py:369, `daemon` src/jarvis/daemon.py:4344, `ops._diagnose_holds`
src/jarvis/ops.py:2005. `state_durations` is the omission, not a different opinion.

Root cause: `Span` was defined with a single duration (`Span.seconds`,
src/jarvis/ops.py:1690) at a time when the wall/active distinction already existed in
`holds`, and nothing forces a new timing reader to consult it. This spec fixes the
reader. The structural root cause — nothing makes a duration declare its basis — is OUT
OF SCOPE here and is worth an `io` later.

Not the problem, and not to be "fixed": the absence of a `held` status. Re-derivation
from the timeline is the design (holds.py module docstring), and Neo question 1130
confirmed the display rule below.

## The fix

Decided already, stated as given, not re-opened: no new work-order status and no new
column; the hold is re-derived from the timeline. The DISPLAY headline is ACTIVE, with
wall as a secondary key that is never deleted (Neo 1130): `running 45m (4h15m held by a
fleet usage limit, 5h24m wall)`. While a hold is open the current-status line reads as
HELD.

### 1. Where the subtraction happens

`state_durations` reads `holds` ITSELF, once, at src/jarvis/ops.py:1892 (after `now` is
resolved, before the span loop), and passes the resulting `tuple[Hold, ...]` into
`StateDurations` as a new field `holds: tuple[Hold, ...] = ()`. `as_dict` does the
arithmetic per span with `Hold.overlap(span.entered, span_end, now)` and
`holds.by_cause(self.holds, start, end, now=now)`.

Why the reader and not the caller: `state_durations` is documented as "one indexed read
per table … no model, nothing written" (src/jarvis/ops.py:1886) and `holds.held` is one
indexed `list_events` plus one `list_turns` — it keeps that contract. There are five call
sites (`cli._readable_time_in_state` src/jarvis/cli.py:309, the two UI pages
src/jarvis/ui/app.py:1081 and :1299, plus `ops.show_work_order` / `show_feature_order`
which build `time_in_state`); making each pass spans in means five places that can forget,
which is exactly how this bug happened. REJECTED: caller-injected spans, and an optional
`spans=` parameter — an optional parameter defaulting to "no holds" reproduces the bug
for every caller that does not opt in.

Store the `Hold` objects on the dataclass rather than pre-computed seconds because
`StateDurations` deliberately bakes in no `now` (src/jarvis/ops.py:1697) and `Hold` has
the same property (`Hold.ended is None` while open, holds.py:156).

FEATURE ORDERS. `holds.held` takes a work-order id, and a feature order has no turns,
no timeline and no `wo_events` rows of its own — `wo_events.wo_id` is a real foreign key
into `work_orders` (project_store.py:4127). The fo case follows `_last_activity`'s family
walk EXACTLY (src/jarvis/ops.py:1853-1877, trap 3 of kn-b7591ab3): `feature_children` +
`plan_wo_id` + `manager_work_order`. It is NOT skipped — leaving fo wall-only while wo
gains an active headline is the mixed basis §4 forbids.

New function in `holds.py` (not in `ops`: this is holds' arithmetic law — merge overlaps,
clip to gaps between turns):

```python
def held_family(store: ProjectStore, wo_ids: Sequence[str], *,
                now: float | None = None) -> list[Hold]:
```

Contract, in three steps and in this order:

1. per member, `_episodes(store.list_events(id))` — the raw pairs, NOT `held()`, because
   `held()` clips to that member's OWN turns only;
2. clip the union against `_working` over EVERY member's `list_turns` merged together:
   a feature is held only where NO family member could run. A child held by the usage
   limit while a sibling types is not a held feature — same doctrine as `_last_activity`;
3. `_merge` over the result: overlaps counted once, earliest start wins, `_RANK` breaks
   the tie (holds.py:339-358). Two children held by the same fleet usage limit is the
   common case and must count once, not twice.

Consequence, stated rather than hidden: a feature whose only live child is held reads as
held even if a sibling is `pending` and undispatched. That is correct by the enumeration
in the holds docstring ("a dependency edge holds nothing measurable") and by the family
doctrine. REJECTED: intersection over children (held only when every child is held) —
a per-cause breakdown over an intersection is not attributable to one cause, and a
feature with one finished child would never read as held again. REJECTED:
`carrier_for_feature` (project_store.py:4123) — trap 3 of kn-b7591ab3, and the same
reason `_last_activity` does not use it.

Cost for an fo: one `list_events` + one `list_turns` per family member. Bounded by the
child count, taken once per page render, no model. Acceptable at the fo page's existing
budget (it already builds `validation_detail` on the same connection, app.py:1078).

### 2. The payload

Every existing key keeps its current meaning: WALL. Nothing downstream silently changes
number. New keys only.

`Span` gains no fields; the arithmetic is `as_dict`'s, off `StateDurations.holds`.

Per span (src/jarvis/ops.py:1732-1738), added:

```
"held_seconds":   round(held_in_span, 2),
"held_human":     ago_phrase(held_in_span),
"active_seconds": round(max(0.0, s.seconds(now) - held_in_span), 2),
"active_human":   ago_phrase(active_seconds),
"active_share":   active_share(active_seconds),
```

Per total (src/jarvis/ops.py:1745-1747), the same five keys, summed over that status's
spans — summed per span and not recomputed over a synthetic window, so a status entered
twice cannot double-count a hold that sits between its entries.

Top level:

```
"lifetime_held_seconds":   round(total_held, 2),
"lifetime_active_seconds": round(lifetime - total_held, 2),
"lifetime_active_human":   ago_phrase(...),
"current_status_held_seconds": float | None,
"current_status_active_age":   float | None,
"current_status_active_age_human": str | None,
"held_now": {...} | None,
"current_status_held_by": [ {"cause","phrase","seconds","seconds_human"}, … ],
```

`held_now` is the current-status block and is `None` unless a hold is OPEN at `now` (a
`Hold` whose `ended is None`, the newest such): `{"cause","phrase","since","seconds",
"seconds_human"}`. `None` — never a zero-filled dict — for the same reason `as_dict`
already gives `None` ages: a missing figure is not a zero (issue #227).

`current_status_held_by` is `holds.by_cause(self.holds, current_status_since, now,
now=now)` rendered as a list, biggest first, EMPTY LIST for an order never held during
its current status. All three `current_status_*` new keys are `None` when
`current_status_since` is `None`, matching `current_status_age` (ops.py:1748).

Both `lifetime_*` figures are computed over the same `[spans[0].entered, end]` window
`lifetime` uses (ops.py:1727), so `lifetime_active + lifetime_held == lifetime` holds by
construction and is a test.

### 3. Renderers

`cli.time_in_state_lines` (src/jarvis/cli.py:278-306), `_states.html` macro
`view()` (src/jarvis/ui/templates/_states.html:13-87) used by both
src/jarvis/ui/app.py:1081 and :1299. The renderer formats NOTHING (cli.py:283): every
number below is read off the payload, `ago_phrase` / `fmt_dur` applied.

NEVER HELD — `total["held_seconds"] == 0` — renders EXACTLY as today, byte for byte:

```
▶ running            5h24m  1 entry     72%   ← now, 2h ago, nothing since 3m ago
```

No suffix, no parenthesis, no extra column. This is the common case and it must not
acquire noise; a golden-string test pins it (§5).

PREVIOUSLY HELD, RUNNING NOW — `held_seconds > 0` and `held_now is None`. The status
duration column shows ACTIVE; wall moves into the parenthesis:

```
▶ running              45m  1 entry     72%   (4h15m held by a fleet usage limit, 5h24m wall)   ← now, 2h ago, nothing since 3m ago
```

Cause phrasing is `Hold.phrase` (holds.py:184) verbatim, taken from the biggest entry of
`current_status_held_by` for the current status and of that status's own breakdown
otherwise. Two or more causes: `held by a fleet usage limit and 2 others`. The bar in
`_states.html:44` is driven by `t.share` (wall) — unchanged, see §4 — with a second inner
bar of width `t.active_share * 100` in the same colour at full opacity and the wall
remainder at reduced opacity, `title="{{ t.held_human }} held"`.

HELD RIGHT NOW — `held_now is not None`. The current-status marker says HELD, not `now`:

```
▶ running              45m  1 entry     72%   ← HELD 4h15m by a fleet usage limit, running 45m of 5h24m
```

`_states.html:31-37`'s `<span class="sub">` for the current status becomes, when
`states.held_now`:

```
held {{ fmt_dur(states.held_now.seconds) }} by {{ states.held_now.phrase }} ·
active {{ fmt_dur(states.current_status_active_age) }} of {{ fmt_dur(states.current_status_age) }}
```

and is otherwise untouched. The tab badge (work_order.html:494) switches to
`states.current_status_active_age_human` when it is not `None`, falling back to
`current_status_age_human` — a badge reading 5h on an order that worked 45m is the
headline complaint of the issue.

`_states.html:75-79` (the per-span Gantt line) gains, only when `s.held_seconds`:
` · {{ fmt_dur(s.held_seconds) }} held`. Nothing is removed.

### 4. `share` stays on WALL

`share` (src/jarvis/ops.py:1729-1730) keeps its denominator and its numerator: wall
seconds over wall lifetime. `active_share` is active seconds over
`lifetime_active_seconds`. Each basis is internally consistent and each sums to 1 across
the spans; a `share` whose numerator were active and whose denominator wall would sum to
less than 1 and silently invite the reader to look for the missing time.

Why wall for the EXISTING key rather than redefining it: `share` is already rendered as a
proportional bar (`_states.html:44`, `:69`) laid out against `lifetime_seconds` and
offset by `(s.entered - spans[0].entered) / lifetime_seconds` (`_states.html:61`). Those
offsets are wall positions on a wall timeline; an active-basis width against a wall-basis
offset draws bars that do not line up. The headline number is active (§3); the GEOMETRY
stays wall, which is also what makes the held portion visible as a gap.

### 5. Tests

Pinned user rule: TARGETED tests only, never the full suite. Run
`uv run pytest tests/test_time_in_state.py tests/test_holds.py -q` while working.

`tests/test_time_in_state.py` (helpers already present: `backdate` line 40, `event_at`
line 54, `turn_at` line 62; `wo_state_spans` carries `order_id` + `order_kind` and no FK,
and `create_work_order` / `create_feature_order` write the first span themselves, so a
fixture back-dates rather than inserts):

1. `test_usage_limit_hold_is_not_running_time` — issue 887 reproduced: `running` at T0,
   `turn_at(T0, T0+45m)`, `turn_paused` at T0+45m, `turn_resumed` at T0+5h24m. Assert
   the `running` total `seconds == 5h24m` (UNCHANGED), `held_seconds == 4h39m`,
   `active_seconds == 45m`.
2. `test_open_hold_reads_as_held` — no `turn_resumed`. `held_now["cause"] ==
   PAUSE_USAGE_LIMIT`, `phrase == "a fleet usage limit"`, and
   `current_status_active_age < current_status_age`.
3. `test_never_held_payload_is_unchanged` — no pause events: every new key is
   `0` / `None` / `[]` and `active_seconds == seconds` for every span and total.
4. `test_active_and_held_sum_to_wall` — per span, per total, and
   `lifetime_active + lifetime_held == lifetime_seconds`.
5. `test_hold_inside_a_running_turn_is_not_subtracted` — a `message_queued` /
   `message_delivered` pair wholly inside `turn_at`: `held_seconds == 0`. Guards the
   clip law (holds.py `_outside`); deleting it would delete real work.
6. `test_feature_hold_is_the_familys` — fo with two children: child A held T0+1h..T0+4h,
   child B running a turn T0+2h..T0+3h. Feature `held_seconds == 2h`, not 3h.
7. `test_feature_counts_one_fleet_hold_once` — both children paused on the same window:
   `held_seconds` equals the window, not twice it, and `current_status_held_by` has one
   entry.
8. `test_planner_and_manager_count_as_family` — a hold on `plan_wo_id` and one on
   `manager_work_order` both reach the fo figure (pins the walk against
   `_last_activity`).
9. Renderer, in the same file beside the existing `cli.time_in_state_lines` cases:
   golden strings for all three §3 lines, the never-held one asserted character for
   character against today's output.
10. `tests/test_rules_cli.py:152`'s `sources` assertion: add a case that a condition on
    `seconds_in_status_active` reports source `state_durations`.

MUTATION CHECK. Two, both required to fail:

* delete the `Hold.overlap` subtraction in `as_dict` (make `held_in_span` always `0.0`):
  cases 1, 2, 6, 7, 8 and the held golden strings fail; case 3 still passes, which is
  what proves case 3 is not the whole test.
* replace `held_family`'s step-2 clip with each member's own `holds.held`: case 6 fails
  with `3h`, everything else passes. That is the fo correctness risk isolated to one
  assertion.

### 6. `rules.FACT_FIELDS`

Add ONE field at src/jarvis/rules.py:211, beside `seconds_in_status`:

```python
("seconds_in_status_active", NUM, "state_durations",
 "since the last status change, minus every interval `holds` says the order was not "
 "permitted to run"),
```

`seconds_in_status` KEEPS meaning wall. Existing rule rows keep their meaning and no
migration is needed; a rule author who wants the new basis opts in by name.

Contract for the field, in `rules.facts`' terms (src/jarvis/rules.py:685-716, declared
and `NotImplementedError` by design — the evaluation pass owns the body, so this spec
adds no code there): `seconds_in_status_active` is ABSENT from `values` exactly when
`seconds_in_status` is absent (no open span — `current_status_since is None`), never
`None` and never `0.0`; it is `as_dict()["current_status_active_age"]`; it equals
`seconds_in_status` for an order never held; it is never greater than it. Same source
slug `state_durations`, so `sources_used` (rules.py:472) already routes it and the
laziness argument is unaffected — one reader, two fields. NUM is already in the
operator table, so the parser needs no change.

One doc line to update: the source list in `rules.facts`' docstring (rules.py:699) says
`state_durations` is `ops.state_durations`; it stays true and needs no edit. Nothing else
changes.

### What this spec does NOT cover

* No new status, column or flag (kn-f1934cfc rule 2).
* `ops.waiting_on`, `invariants`, the attention list: untouched. An order held by a usage
  limit already reads correctly there.
* `jarvis inspect` / `autopsy`: already hold-aware.
* The structural root cause (nothing forces a new timing reader to declare its basis).
* Back-filling any stored number — every figure here is computed at read time
  (kn-96f47efb).
