# Cost page: aggregate overview first, order list on demand and paginated

Feature order fo-4a6386eb, from improvement order io-2641fe05 (findings
`ledger-not-overview`, `work-order-grain`). Read this file by section:
`jarvis spec section docs/superpowers/specs/2026-11-09-cost-overview-first.md <n>`.

## 1. Why, what is fixed, and the shared interfaces

### 1.1 The problem

`/cost` (src/jarvis/ui/templates/cost.html) opens with six paragraphs of accounting
prose, then every order in the window (235 rows, 385 KB of HTML, no pagination). It
answers no aggregate question: which project spends most, how many orders of each kind,
how many cache-TTL misses. The figures exist on every unit (`boundaries_ttl`,
`rewrite_ttl_excess`) and are never summed per project. The tally above the list
(cost.html ~159-176) sums every row SHOWN, which is why pagination was never added:
paginate and the tally stops matching the headline.

### 1.2 Fixed by the user (do not re-litigate)

- Overview first, rows on demand (dashboard rule kn-fa085e88: one primary view at full
  width, secondary content on demand).
- Fleet: total $, $ per project, orders by kind per project. Per project: order count,
  kind split, TTL-miss count, TTL re-write tokens.
- Aggregates are computed in `ops` from the SAME units `ops.cost_report` returns. No
  second ledger, no second walk over transcripts.
- Pies carry their legend INSIDE the chart. Inline SVG. No CDN, no new JS dependency.
- The order list is collapsed and server-paginated through URL query parameters.
- The headline prose (cost.html ~86-155) is shown on demand, not by default.
- Everything is computed by `CostCache` (src/jarvis/ui/app.py); the request path never
  aggregates.
- `jarvis cost --json` carries the aggregates.

### 1.3 The sibling this builds on (not part of this feature)

Work order wo-de40eb46 changes `ops.cost_report` so each unit is the ROOT order of any
kind (wo/fo/io/do/inv/mo), its total = own + every descendant, its children kept on the
unit for expand. This feature consumes the top-level `units` list ONLY and never
descends into a unit's children: that is what keeps every dollar counted once. If
wo-de40eb46 has not merged when a section starts, the code below still works on the old
grain (every unit a wo-) because the order kind is read from the id prefix (§1.4).

### 1.4 Shared interface: `report["aggregates"]`

Built by `ops.cost_aggregates(units, unattributed)` (section 2) and attached by
`ops.cost_report` to its window/list return value as the key `aggregates`. Shape:

```python
{
  "fleet_cost_usd": float,       # == report["totals"]["total_cost_usd"]
  "orders": int,                 # len(units): top-level units only
  "by_order_kind": {"wo": int, "fo": int, ...},   # fleet-wide order counts
  "projects": [                  # dearest first
    {"project": str,
     "cost_usd": float,          # _rollup(this project's units)["totals"]["total_cost_usd"]
     "orders": int,
     "by_order_kind": {"wo": {"orders": int, "cost_usd": float}, ...},
     "ttl_misses": int,          # sum of unit["boundaries_ttl"] over found units
     "ttl_rewrite_tokens": int}, # sum of unit["rewrite_ttl_excess"] over found units
  ],
  "unattributed_cost_usd": float,  # report["os_unattributed"]["os_cost_usd"], or 0.0
  "rounding_usd": float,  # fleet_cost_usd - (sum(projects.cost_usd) + unattributed_cost_usd)
}
```

- `order_kind` is the id prefix before the first `-` (`wo-…` gives `wo`, `inv-…` gives
  `inv`). Never reuse the word `kind`: `_unit_row` already has a `"kind"` field holding
  the worker role (planner, analyst, …).
- A project's dollars come from calling `_rollup` on that project's units, never from
  summing `u["total_cost_usd"]`: `_rollup` is the headline's own definition (worker
  list cost of found units only, OS and subprocess spend of all units), so a slice
  follows whatever the headline does, including after the sibling's rollup.
- `unattributed_cost_usd` is its own slice ("no order") wherever spend is split by
  project. It belongs to no project.
- `rounding_usd` exists because `_rollup` rounds its components before adding them, so
  rounded slices need not sum exactly to the rounded headline. It is reported, never
  forced to zero by adjusting a slice.
- Keys are additive: no existing key of the report changes.

## 2. Aggregates in ops, carried by the cache and the CLI

Add `cost_aggregates` in src/jarvis/ops.py next to `_rollup`, a pure function of its
arguments (no store, no transcript read). Call it on `cost_report`'s list-path return
line and add `"aggregates"` to the returned dict — keep the touch to that line, so the
merge with wo-de40eb46 (which rewrites the loop above it) stays trivial. The `target`
path (`_cost_for_target`) is untouched.

- Per-project figures as §1.4 says. TTL figures sum only units with `found` true, the
  same filter `_rollup` uses for `rewrite_excess`. `_rollup`'s totals carry neither TTL
  key; read them off the units (`Usage.as_dict` puts them there).
- Aggregates never read a unit's `children`. If, after wo-de40eb46, a root unit's TTL
  fields do not include its descendants, fix that where the root unit is built, not here.
- `CostCache` stores whole `cost_report` returns, so the aggregates are cached with no
  cache change. The snapshot on disk (`cost_snapshot_path`) holds old-shape entries:
  bump `SNAPSHOT_VERSION` in src/jarvis/ui/app.py. The bump renames the file, so the
  first boot after release shows the computing page once; that is accepted.
- `jarvis cost --json` prints the `cost_report` return value, so it carries
  `aggregates` with no CLI change. The text output of `jarvis cost` does not change.
  Without a window the CLI lists `limit` orders per project; aggregates and headline
  share that limit and stay consistent.

Tests (a new tests/test_cost_aggregates.py, synthetic unit dicts):
- `sum(projects.cost_usd) + unattributed_cost_usd + rounding_usd ==
  pytest.approx(fleet_cost_usd)`, `fleet_cost_usd ==
  _rollup(units, unattributed)["totals"]["total_cost_usd"]`, and
  `abs(rounding_usd) < 0.015 * (len(projects) + 1)` (`_rollup` rounds worker, OS and
  subprocess parts separately, so each slice can be off by up to three half-cents; the
  per-project `_rollup` call passes `unattributed=None`; the unattributed figure is
  unrounded, so compare floats with `approx`, never `==`);
- an unfound unit with OS spend: its OS dollars land in its project, its TTL figures
  do not;
- order kinds counted by id prefix across mixed wo/fo/io/inv ids; a unit carrying a
  `children` list is counted once and its children never;
- `jarvis cost --json` output contains `aggregates` (model it on
  `test_the_json_payloads_carry_the_window_and_one_meter_subtree` in
  tests/test_cost_meter_payload.py, which drives `cli.main(["cost", "--json"])`);
- tests/test_cost_report.py passes unchanged.

## 3. Overview section of the cost page, with its pie chart

### 3.1 Pie macro

A Jinja macro file src/jarvis/ui/templates/_pie.html exporting
`pie(slices, title, fmt)` where `slices` is a list of `{"label": str, "value": number}`
and `fmt` is `'usd'` or `'count'`. Server-rendered: one `<svg>` element, no `<script>`,
no external asset.

- Slices sorted largest first; beyond 6 slices the tail collapses into one "other".
- The legend is SVG content beside the pie in the same `<svg>`: a swatch `<rect>` and a
  `<text>` per slice (label, value by `fmt`, percentage). Not a caption, not HTML.
- Colours: a fixed palette of CSS custom properties with dark-mode values (follow how
  base.html defines its tokens); "other" and "no order" use the muted ink.
- A single slice is a `<circle>` (a 360-degree arc renders nothing). A zero total draws
  the title and "nothing in this window".
- Each slice has a `<title>` for hover.
- Arc maths in a small Python helper (e.g. `pie_arcs(values)` returning path `d`
  strings) registered as a Jinja global, so it is testable without rendering.

### 3.2 The overview

In src/jarvis/ui/templates/cost.html, the headline panel (~86-155) becomes:

- Headline: `~$` fleet total and order count, as now.
- Fleet row, only when the page is not scoped to one project: pie of $ by project
  (with the "no order" slice), pie of orders by kind.
- One block per entry of `aggregates.projects`: order count, order-kind pie (counts),
  TTL misses, TTL re-write tokens (`fmt_tok`). On a `?project=` page this is the only
  block (a one-slice $ pie says nothing).
- Reconciliation line: project slices + "no order" + rounding = the headline, from the
  `aggregates` figures. It REPLACES the shown-rows tally above the list (cost.html
  ~159-176): delete that tally here. Keep the "Not in that total: … sessions Jarvis did
  not dispatch" line.
- The existing prose (workers / jarvis / subprocesses split, undated messages, OS-call
  explanation, unpriced note, re-write tax, subagents) moves verbatim into a
  `<details>` closed by default, summary "How this total is made". Do not rewrite it.
- An entry without `aggregates` must not crash the template: headline only.

Do not touch the order-list table, the route, the meter include or the
fleet-distribution include.

Tests: `pie_arcs` (one slice is a `<circle>`, two equal slices, zero total, a 7+ slice
tail); the macro renders one `<svg`, one legend `<text>` per slice, no `<script`; in
tests/test_ui_cost.py the page renders pies and per-project TTL figures, the prose sits
in a closed `<details>`, the reconciliation line shows the headline figure, the old
"below add to" tally is gone, and an entry without `aggregates` still renders.

Existing tests this section breaks, to rewrite against `aggregates` (not delete
silently): `test_the_order_list_comes_first_and_its_tally_adds_to_the_headline`,
`test_the_tally_names_the_rounding_between_rows_and_headline` (now against
`aggregates.rounding_usd`), and `test_calls_no_work_order_caused_get_a_row_and_a_true_sentence`
(its tally sentence), all in tests/test_ui_cost.py. Also delete section 4's
"tally unchanged by paging" test if it has merged: the tally it guards is gone.

Trap: tests/test_ui_cost.py has no stubbed entry. `seed_cache` stores a real
`cache.compute(...)`, and `_wrap` edits `cost_report`'s return afterwards, so a test
that changes `totals` through `_wrap` leaves `aggregates` stale. Recompute
`aggregates` in the wrapper (call `ops.cost_aggregates`) or build the entry yourself.

## 4. Order list: collapsed and server-paginated

- Route `cost_page` (src/jarvis/ui/app.py) gains `page: int = 0` (0 = list collapsed).
  Rows are SLICED from the cached entry's `report["units"]` at render time; nothing is
  recomputed. `page` must not enter the cache matching rule (the
  `if (not project and offset …` test before `cost_cache.lookup`) nor `CostCache.key`:
  a page click hits the same entry as the overview.
- Page size: module constant `COST_PAGE_SIZE = 50`.
- The list sits in a `<details>`, closed when `page` is 0, open when `page >= 1`; the
  summary states the count ("235 orders, dearest first"). With `page` 0 the closed
  `<details>` still contains the page-1 slice and its links, so opening it needs no
  request and existing tests that read rows off `/cost` keep passing.
- Prev/next and "page n of m"; every link keeps `project`, `window`, `offset`,
  `since`, `until`, `tz`. Out-of-range pages clamp.
- The "no work order" row (os_unattributed) shows on the last page.
- The shown-rows tally above the list (cost.html ~159-176) belongs to section 3, which
  deletes it. Until that lands it must stay correct: keep it summing the FULL `units`
  list, not the page slice. Pass the slice to the table under a new template variable;
  do not rebind `units`.
- Keep whatever per-row expand of a root's children wo-de40eb46 added; pagination is
  over top-level units only.

Tests in tests/test_ui_cost.py: page 1 shows at most 50 rows (`COST_PAGE_SIZE`), page
2 the rest; links keep `window`, `project` and `tz`; a `page` request is served from the
cached entry with no compute started (`seed_cache` plus the `count_live` helper); no
`page` leaves the `<details>` closed; the no-work-order row is on the last page; if the
"below add to" tally is still on the page, its figure is unchanged by paging (skip the
check when the tally is absent: section 3 deletes it). Existing tests stay green.

## 5. What stays out

- The usage-meter legend (finding `chart-legend-in-prose`) is a separate order; it may
  later reuse §3.1's macro or replace it with a shared helper.
- Re-write units on rows vs headline (finding `rewrite-units-mismatch`) is a separate
  order.
- The text output of `jarvis cost` gains no overview.

## Agent profile

You are a worker on the Jarvis OS repository (Python 3.11+, FastAPI + Jinja dashboard,
SQLite stores), building one section of the cost-page overview feature. Your section of
docs/superpowers/specs/2026-11-09-cost-overview-first.md is your brief; read §1 (the
shared interfaces) with `jarvis spec section <path> 1` before you code, and never read
the spec whole.

What you must know:
- `ops.cost_report` (src/jarvis/ops.py) returns `units` (one per order, dearest first),
  `totals` (from `_rollup`; the headline is `totals.total_cost_usd`) and
  `os_unattributed` (OS spend no order owns). The headline is workers' list cost of
  found units + every unit's and the unattributed OS cost + subprocess cost. Any figure
  you add must sum back to that headline.
- A sibling order (wo-de40eb46) is making each unit a ROOT order with children. Read
  only the top-level `units`; never sum `children`. Derive the order kind from the id
  prefix.
- The dashboard never computes on the request path. `CostCache` in
  src/jarvis/ui/app.py precomputes `{report, fleet, meter}` per window; routes read
  entries. A new query parameter that is not a window bound must not change the cache
  key or the matching rule.
- Templates live in src/jarvis/ui/templates; there is no static asset directory and no
  CDN. Charts are inline SVG rendered server-side.
- Navigate code with Serena symbol tools (`find_symbol`, `find_referencing_symbols`);
  the Bash hook refuses grep over Python.

Conventions:
- Smallest change that fully solves your section. Additive payload keys; never rename
  an existing key — `jarvis cost --json` consumers and the snapshot read them.
- Comment only the why, in one line. Money is formatted `'%.2f'` with a `~$` prefix as
  the page does today; tokens through `fmt_tok`.
- Run only the targeted tests (`uv run pytest tests/test_cost_report.py
  tests/test_ui_cost.py -q` or the file you added). Never the full suite; CI runs it.
- Open the PR with the `open-a-pull-request` skill; the body needs "## How to validate
  in production".

Traps:
- Rounding: `_rollup` rounds components before adding. Report the residual
  (`rounding_usd`), never force it to zero by fudging a slice.
- `kind` on a unit is the worker role; the order kind is `order_kind`, from the id
  prefix.
- The snapshot file survives restarts; a shape change needs `SNAPSHOT_VERSION` bumped.
- An SVG arc of 360 degrees draws nothing; a single slice is a `<circle>`.
- Do not touch `_meter.html`, `_fleet_distribution.html`, pricing maths, or
  `_cost_for_target`. Those belong to other orders.

Never: poke the SQLite databases or the production checkout; run the full test suite;
merge your own PR without the gate; widen your section because a neighbour's looks
unfinished — ask Neo (`jarvis wo ask`) instead.
