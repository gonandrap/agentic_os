# Every filtered list view sorts newest first by a status-change timestamp

Feature order fo-eace4762, from improvement order io-f2617e9d (findings attention-pin,
per-view-ordering, settled-time-key). Knowledge: kn-7fa1f3f0, kn-e6de96ad, kn-323d3695,
kn-eb7be693.

## 1. The rule and why it breaks today

User rule: every filtered view in `src/jarvis/ui/app.py` (every route that calls
`query_filters()`) lists newest first. The user's report: `/project/jarvis_os?type=feature&status=completed`
showed 34d, 38d, 43d … first, then 2h, 12h, 2d … — "random".

Three causes, all confirmed in code:

1. `attention_first()` (app.py, next to `filter_rows`) runs on every row of `project()` and
   `dashboard()`, settled rows included. A completed feature whose child carries a
   month-old escalated alarm stays on top of every "completed" view forever.
2. No settled-time key exists. `work_orders` / `feature_orders` hold only `created_at` and
   `updated_at`; `updated_at` moves on any write (alarm, ack, hide). The real transition
   time lives only in `wo_state_spans`. `list_work_orders` / `list_feature_orders` run
   `ORDER BY created_at DESC LIMIT ?` in SQL, so any Python re-sort after the LIMIT drops
   old-created, newly-settled orders off page 1.
3. No shared ordering primitive. Each route orders rows its own way (gates by state rank,
   morpheus by `updated_ts`, `project()` open rows by kind then status), so nothing stops a
   new view from breaking the rule.

Done means: (1) a `status_since` key in SQL before LIMIT; (2) one primitive every filtered
route uses; (3) attention pins only on open views, a badge on settled ones; (4) open orders
of all kinds in one time-ordered list; (5) the age column shows the sort key; (6) a test
over every `query_filters()` route. **What counts as attention does not change** — every
`needs_you` predicate in app.py stays exactly as it is; only WHERE it applies changes.

## 2. Definitions shared by every section

- **`status_since`** — when an order entered its current status: the latest
  `wo_state_spans.ts` for that order id, else `created_at`. Same epoch seconds as
  `created_at`. Never `updated_at`. `wo_state_spans` (project_store.py, table DDL near the
  `idx_state_spans_order (order_id, ts)` index) gets a row on every WO transition
  (`create_work_order`, `claim_next_pending`, `set_status`) and every FO transition
  (`create_feature_order`, `set_feature_status`; old FOs are backfilled with
  `approximate=1`). Order ids are prefixed (`wo-`, `fo-`, …) so `order_id` alone is unique;
  the existing invariant `check_spans_reach_the_status` keeps the tail span equal to the
  status.
- **Open view vs settled view** — a view is SETTLED when its status/state filter names a
  terminal value: `past`, any of `TERMINAL_STATUSES` / `FO_TERMINAL_STATUSES` (completed,
  cancelled, failed, …), or the per-page equivalent (morpheus `state=failed|timedout|…`
  terminal states, release `status=failed|validated|…`, steward `tab=settled`, backlog
  `status` in `SETTLED_BACKLOG_STATUSES`, gates a decided state, alarms a decided state).
  Every other view — no filter, an open status, `all` — is OPEN.
- **Pin** — on an OPEN view, rows matching the page's existing `needs_you` predicate move
  to the top as a group; inside each group rows stay newest first. On a SETTLED view there
  is no pin: rows are strictly newest first, and a row that matches `needs_you` shows a
  badge (the existing attention styling on the row, e.g. the `needs you` / reason chip the
  open view already renders) instead of a position.
- **The primitive** — `newest_first(rows, key, needs_you=None) -> list[dict]` in app.py,
  next to `attention_first`:
  - sorts descending by `key(row)`; a row whose key is `None`/0 sorts LAST; ties keep
    incoming order (stable sort);
  - then, only when `needs_you` is given, applies `attention_first(rows, needs_you)`;
  - callers pass `needs_you=None` on a SETTLED view.
  `attention_first` itself is unchanged (tests/test_ui.py
  `test_the_list_page_primitives_filter_and_order_rows` covers it). After this feature, no
  route in app.py that reads `query_filters()` sorts its list rows any other way.

## 3. Sort key in SQL and the ordering primitive

The foundation; sections 4 and 5 import from it.

- `ProjectStore.list_work_orders` and `ProjectStore.list_feature_orders`
  (src/jarvis/project_store.py) ALWAYS select an extra column
  `COALESCE((SELECT MAX(s.ts) FROM wo_state_spans s WHERE s.order_id = <table>.id), <table>.created_at) AS status_since`.
- Both take a new keyword `order_by: str = "created_at"`. Only `"status_since"` changes the
  SQL to `ORDER BY status_since DESC, created_at DESC, id DESC LIMIT ?`. Any other value is
  refused (`ValueError`) — it is interpolated into SQL, so it is a closed set. The default
  keeps the ~20 existing non-UI callers (daemon `_health_subjects`, `settle_turns`,
  `ops.cost_report`, `_orders_in_window`, `_open_release_orders`, tests) byte-for-byte on
  their current order.
- `ops.os_status` (the dashboard's source): pass `order_by="status_since"` where it lists
  open WOs and open FOs for each project, and add `"status_since"` to both the
  `feature_orders` and `open_work_orders` row dicts it builds (today they carry no
  timestamp at all).
- `ops.stuck_report`: add `"status_since"` to each row (the time it already computes as
  the entry into the current status). Do not change its own sort; the CLI renders it.
- Add `newest_first` to app.py exactly as defined in section 2. It is not wired into any
  route here.
- Tests: the SQL key (an order created long ago that settled just now lists before one
  created yesterday that settled yesterday; with `limit=1` the recent settle is the one
  returned — proving ordering happens before LIMIT; an order with no spans falls back to
  `created_at`; default `order_by` order unchanged), `os_status` rows carry
  `status_since`, and `newest_first` (descending, None last, stable, pin only when given).

## 4. The non-order list routes

Convert each of these routes in `create_app` to order its listed rows with `newest_first`
and nothing else. Keep every filter, option list, count and template contract. The time
key per route — the worker verifies each field exists on the row before using it; where a
row carries a status-change time, prefer it over a creation time:

| Route | Rows ordered | Key | Pin on open view (existing predicate, unchanged) |
|---|---|---|---|
| `search_page` | `hits` | see 4.1 | none |
| `inbox` | `items` | `ts` | see 4.1 |
| `backlog` | `rows` | status-change time if the row has one, else `created_at` | `bool(i["blockers"])` |
| `knowledge` | `rows` | `ts` | none |
| `neo_page` | `history` only (in_flight/escalated/unreviewed are queues, untouched) | `answered_at` else `ts` | none |
| `gates_page` | `every` | `ts` | today's state rank: escalated, with neo, awaiting_case (replace the `rank` sort with the pin; keep the grouping of those three open states) |
| `alarms_page` | `history` | decision time if present, else `ts` | `state == "escalated"` |
| `stuck_page` | `rows` | `status_since` (from section 3) | `r["stuck"]` |
| `morpheus_page` | `rows` | `updated_ts` | owed_by user, then failed/timedout — open views only |
| `release_page` | `shown_pending`, `shown_releases` | `ts` | releases: `status == "failed"` |
| `release_item_page` | `rows` | the row's time field | `validation == "failed"` |
| `steward_page` | each tab list | `ts` | none (the `asks` tab IS the attention) |
| `_evolution` | `shown` | `last_fired` (None last) | `not r["readable"]` |

`requestor_query` is a helper, not a route: it filters and orders nothing.

Settled detection per route follows section 2 "Open view vs settled view": compute it from
that route's own filter value and pass `needs_you=None` when settled.

### 4.1 Views that already rank by something other than time

Pending a ruling recorded on wo-eead75ce (Neo): `inbox` has `?sort=` with
`ops.INBOX_SORTS = ("priority", "newest", "oldest")`, default `priority`; `search_page`
ranks hits by relevance to `q`. Planner's recommendation: inbox DEFAULT becomes `newest`
(priority stays selectable; the separate `critical` section remains the pin); search keeps
relevance when `q` is non-empty and is newest first when `q` is empty. Follow the ruling
recorded on fo-eace4762 / wo-eead75ce if it differs.

## 5. The order views: dashboard and project page

`dashboard()` and `project()` in app.py, plus `templates/dashboard.html` and
`templates/project.html`.

- **One list.** Open orders of every kind (work, feature, improvement, investigation,
  decision) interleave in ONE list ordered by `status_since` via `newest_first`. Today
  both routes put `families` first, then `group_open()`'s featured statuses, then the rest;
  that grouping-as-order goes. `group_open` stays only as the FILTER it also is on the
  dashboard (statuses that collapse into `rest_counts` stay collapsed unless revealed);
  `project()` calls it with `"all"`, so there it is order only and is dropped.
- **SQL order.** `project()` passes `order_by="status_since"` to both
  `list_work_orders` and `list_feature_orders`. Each returns its top `cap`; merging two
  top-`cap` lists by the same key and truncating to `cap` is correct paging. Delete the
  `past = sorted(..., key=created_at)` re-sort.
- **Pin only on open views.** The pin predicate stays
  `status in NEEDS_YOU_STATUSES or needs_attention`. `project()` is SETTLED when its
  `status` filter is `past` or a terminal status; then `needs_you=None` and settled rows
  carrying `needs_attention` render a badge. `status=all` is OPEN (pin applies). The
  dashboard lists only open orders, so it always pins.
- **Age column = sort key.** `project.html` shows `fmt_age(o.status_since)` where it shows
  `fmt_age(o.created_at)` today, with `fmt_ts(o.created_at)` kept reachable (title
  attribute) so creation time is not lost. The dashboard row gets the same age cell
  (it has none today). The dashboard live-refresh swaps regions whose text changed, so the
  age text changing every poll is expected, not a bug.
- **Tests** for: the reported URL shape (`type=feature&status=completed`) lists by
  completion time newest first even when an older row has `needs_attention`, with a badge
  on it; an open view still pins; kinds interleave by time; the age cell shows
  `status_since`; paging with `limit` keeps a recently-settled old order on page 1.

## 6. The cross-route guard test

One table-driven test (tests/test_ui.py or a new tests/test_ui_ordering.py) over every
route in app.py that calls `query_filters()`: dashboard, search_page, project, inbox,
backlog, knowledge, neo_page, gates_page, alarms_page, stuck_page, morpheus_page,
release_page, release_item_page, steward_page, _evolution — plus `requestor_query` listed
explicitly as a helper that orders nothing.

- For each route: seed at least two rows with distinct times, request a SETTLED filter (or
  the route's default view where it has no settled filter), and assert rows render newest
  first by that route's key from section 4/5. For routes with a pin, also request an OPEN
  view and assert the pinned group comes first and each group is newest first.
- **Drift guard:** assert the test's route list equals the set of functions in
  `inspect.getsource(create_app)` that call `query_filters(` (count and names), so a new
  filtered route fails CI until it is added to the table with its key.
- Seeding uses the existing UI test fixtures and stores; never the real `JARVIS_HOME`.

## 7. Out of scope

- What counts as attention (every `needs_you` predicate is unchanged).
- CLI list commands (`jarvis wo list`, `fo list`, …): their order is not touched.
- The six month-old escalated alarms on the reported page need the user
  (`jarvis alarms review …`); no code clears them.
- Adding a `settled_at` column: `status_since` is derived, nothing is migrated.
- Detail pages that list children (`feature_order` children, `improvement_order`
  findings) do not read `query_filters()` and keep `attention_first`.

## Agent profile

You are a worker implementing one section of the "filtered views newest first" feature in
the Jarvis OS repository (Python 3.11+, FastAPI + Jinja2 UI in `src/jarvis/ui/`, SQLite
stores in `src/jarvis/project_store.py`, business logic in `src/jarvis/ops.py`). You are
handed one numbered section of
`docs/superpowers/specs/2026-11-09-filtered-views-newest-first.md`; section 2 of that spec
holds the shared definitions — read it with `jarvis spec section <path> 2` before you
code. Your section is your whole scope.

What you must know:
- The sort key is `status_since` (latest `wo_state_spans.ts`, else `created_at`), computed
  in SQL by `list_work_orders` / `list_feature_orders` when called with
  `order_by="status_since"`. Never sort orders by `updated_at`: it moves on unrelated writes.
- The ordering primitive is `newest_first(rows, key, needs_you=None)` in
  `src/jarvis/ui/app.py`. Use it; do not write another `sorted(...)` for list rows in a
  route. Pass `needs_you=None` on a settled view.
- Never change WHAT counts as attention. Each route's existing `needs_you` lambda is kept
  verbatim; you only decide whether it is applied (open view) or shown as a badge
  (settled view).
- Sorting after a SQL LIMIT breaks paging. Order in SQL wherever the store applies a LIMIT.

Conventions:
- Navigate code with Serena symbol tools (`find_symbol`, `find_referencing_symbols`,
  `get_symbols_overview`), not grep. Navigate specs with `jarvis spec toc|section`.
- Run only the targeted tests for what you changed (`uv run pytest tests/<file> -q`).
  Never the full suite: CI runs it on three Python versions.
- New logic gets one small test. Keep diffs minimal; delete code rather than add options.
- Comments say only the why the code cannot show, one line, citing the spec section.
- PR body must contain a "## How to validate in production" section.

Traps:
- `wo_state_spans` is indexed on `(order_id, ts)`; keep the correlated subquery keyed by
  `order_id` so it stays an index seek.
- Shared templates: `_release.html` and `_morpheus.html` are macro files used by several
  pages; order lives in the route, not the template.
- The dashboard collapses some open statuses into a count (`group_open` / `rest_counts`):
  that is a filter, keep it.
- Inbox and search already rank by something other than time — follow the ruling recorded
  for section 4.1, not your own preference.

Never: touch `~/workspace/production`; poke the live SQLite databases; change a
`needs_you` predicate; change the default `order_by` of the store methods (non-UI callers
depend on created_at order); edit a section of this spec that is not yours.
