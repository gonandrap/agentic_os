# Dashboard: full width, failures first, every row opens its record

Feature order fo-744c42a5. Principles recorded as learning kn-fa085e88 (project jarvis_os,
topic ui). This spec **supersedes §5 "Page layouts at width"** of
`docs/superpowers/specs/2026-10-22-a-console-you-can-move-through.md`: the `.flow`
auto-grid it introduced is the cause of the squeeze and is removed.

## 1. Problem and the three rules

`jarvis ui` (`src/jarvis/ui/app.py`, `src/jarvis/ui/templates/*.html`) lays pages out as
side-by-side panels. The `flow()` script in `base.html` turns every `h2` section inside a
`.flow` wrapper into an auto-fit grid cell, so on a wide screen a one-line empty state takes
half the width while an 8-column table wraps one word per line next to it (seen on prod
v0.12.30, 2026-10-29: /morpheus, /stuck, /steward, /evolution). Rows are chronological, so
a failed record is buried, and clicking an id only scrolls.

Every page in this feature follows three rules, decided by the user:

1. **Use space well.** Each page leads with ONE primary view at full width. Secondary
   content (session info, explanations, less urgent sections) goes in tabs (`.tabbed`), a
   `<details>` fold, or a detail page. Long prose never sits in a table column.
2. **Attention first.** Anything failed, timed out or needing the user sorts to the top or
   shows in a pinned "needs you" strip above the primary view. Every list is filterable
   (state, project, severity, as fits) and the filter lives in the query string, so a
   filtered view is a URL.
3. **Progressive disclosure.** Every row's id is a link to that record's own detail route.
   A click never just scrolls the page.

Data rule: new UI code reads through `ops.py` functions (the same layer the CLI calls),
never `CentralStore`/`NeoStore`/`ProjectStore` or SQLite directly. When a section rewrites
a route that today opens a store itself, it moves that read behind an `ops` function; routes
a section does not touch stay as they are.

## 2. The shared foundation

Owned by one child; **the only section that edits `base.html`** and the only one that adds
module-level helpers to `app.py`. Everything later sections use is defined here.

### 2.1 Remove the auto-grid
- Delete `flow()` and its call in `base.html`, and the `.flow`/`.sec` CSS (keep `.grid` if
  the dashboard/project pages still use it directly).
- Remove the `class="flow"` wrapper (and its `--col` style) from all 13 templates that
  carry it: alarm, alarms, investigation_order, morpheus, bill, evolution, feature_order,
  steward, neo, cost, gates, neo_stats, stuck. Pages become a full-width stack; later
  sections reshape them.
- Fix tests asserting the old layout, e.g. `tests/test_steward_queue.py` asserts
  `'class="flow"' in html`.

### 2.2 `app.py` helpers (module level, beside `fmt_age`/`fmt_ts`)
- `query_filters(request, allowed: dict[str, Sequence[str]]) -> dict[str, str]` — reads
  each named query parameter; a value not in its allowed list is dropped (an empty
  sequence means free text: stripped, capped at 100 chars). Never raises on bad input.
- `filter_rows(rows, filters, text_keys=()) -> list[dict]` — equality on each non-empty
  filter; the `q` filter is a case-insensitive substring over `text_keys`.
- `filter_options(rows, key) -> list[str]` — sorted distinct non-empty values.
- `attention_first(rows, needs_you: Callable[[dict], bool]) -> list[dict]` — stable: rows
  needing the user first, each group keeping its incoming order.
All four are registered as Jinja globals where templates need them.

### 2.3 Template macros — new `templates/_layout.html`
Imported as `{% import "_layout.html" as ui %}`.
- `ui.page_head(title, meta="")` with an optional caller body: one compact header line
  (title, counts, state) instead of a key/value panel.
- `ui.needs_you(rows, label)` with a caller rendering one row: a pinned strip above the
  primary view showing the count and each row; renders NOTHING when `rows` is empty.
- `ui.filter_bar(action, filters, options, shown, total)` — a GET form of `<select>`s (and
  a text box when `q` is allowed), the active filters as removable chips, "shown of total",
  and a "clear" link. Changing a select submits (small inline script; works without it via
  a submit button).
- `ui.row_id(id, href)` — the id cell link: monospace, `white-space:nowrap`, class
  `rowid`. Every list row's id uses it.
- `ui.explain(summary)` with a caller body — a closed `<details class="explain">` for
  prose explanations that used to sit above or inside a table.
- `{% block style %}` in `base.html`'s head so pages add CSS without editing `base.html`.

### 2.4 CSS rules (in `base.html`)
- `.table-wrap { overflow-x: auto }`: a wide table scrolls sideways on a narrow screen
  instead of squeezing cells to one word per line.
- `td.num`, `td.when`, `.rowid`: `white-space: nowrap`. `td.prose`: `min-width: 28ch`,
  clamped to two lines (`-webkit-line-clamp`) with the full text in `title` and on the
  detail page.
- `.needs-you` uses the existing `.attention` tones.

### 2.5 Worked example: /inbox
Convert `/inbox` (already filtered by `ops.select_inbox`) to the macros: `filter_bar` for
level/project/q/sort, `needs_you` for critical items, `row_id` linking each item to the
record it is about (via `/go/<id>` when it names one). Its read moves behind an ops
function. This is the pattern every later section copies.

### 2.6 The readability check (browser tests)
Add a fixture `readable` to `tests_browser/conftest.py` returning
`check(page, selector="main td, main th, main p")`: for every matched element whose text is
longer than 30 chars, `lines = round(height / line-height)` and it fails when
`len(text) / lines < 12` (one or two words per line), naming the element's text. Each later
section calls it on its pages at **1440×900 and 390×844** (`page.set_viewport_size`). It
ignores elements inside a closed `<details>` or hidden tab and inside `pre`.

## 3. Morpheus run history (data, no UI)

Today `morpheus_items` (`central_store.py`) keeps only `checks`, `last_exit` and
`last_output`; `morpheus.check()` overwrites them, so there is no history to show.

- New table in `central_store.py`'s schema: `morpheus_runs (id INTEGER PRIMARY KEY,
  item_id TEXT NOT NULL, ts REAL NOT NULL, exit INTEGER, output TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'check', state_after TEXT NOT NULL DEFAULT '')` with an index
  on `(item_id, ts)`. `kind` is `check` (daemon or `jarvis morpheus check` on an open item)
  or `probe` (a settled item run on demand by `ops.morpheus_check`, which never moves
  state). `output` is the same tail `last_output` keeps (`OUTPUT_TAIL`).
- Store methods: `add_morpheus_run(...)`, `morpheus_runs(item_id, limit=50)` newest
  first. Keep the last 50 runs per item: prune older rows in the insert's transaction.
- `morpheus.check()` inserts a run in the same transaction as its `update_morpheus_item`;
  `ops.morpheus_check()` records the probe path.
- `ops.morpheus_detail(item_id) -> dict` with keys `item` (as `morpheus_item`), `runs`,
  `questions` (every Neo question, any status, where `morpheus.item_of_question(q) ==
  item_id`: id, status, kind, question headline, answer headline, ts), `next_check_ts`,
  `retries` (`{"checks", "auto_requeues", "max_auto_requeues"}`), and `handoff` parsed from
  `item["action"]` (`neo:<qid>` / `investigation:<id>` / `improvement:<io-id>` /
  `none:<why>`) as `{"kind", "ref", "why"}`. Raises `OpsError` for an unknown id. Put it
  beside `morpheus_item` in `ops.py`.
- `jarvis morpheus show <id>` prints the last 10 runs (time, kind, exit, state after) and
  the questions, after what it prints today.
- Existing items have no runs; the detail page says "no runs recorded before <release>".

## 4. Morpheus: the reference build

Uses §2's helpers and §3's `ops.morpheus_detail`. Owns `morpheus.html`, a new
`morpheus_item.html`, and the morpheus routes in `app.py`.

### 4.1 The list, `/morpheus`
- `ui.page_head`: "Morpheus", the counts, and ONE line of session state (paused/running,
  turn in flight or idle, held-until). The full session key/value table moves into a
  closed `ui.explain("session")` fold below the primary view, with the intro prose.
- `ui.needs_you`: items in `failed`/`timedout` whose resolution does not start with
  `confirmed failed` and that are not waived — PR, state, one-line why, link to detail.
- `ui.filter_bar`: `state` (STATES), `project` (from the rows); URL `?state=failed`.
- Primary table at full width, `attention_first` then newest `updated_ts` first: id
  (`row_id` to `/morpheus/<id>`), project, PR (`#n`, title in `title`), state, runs
  (`12 · exit 75`, nowrap), deadline or next check (nowrap), handed to (kind + linked ref,
  one line). Script and output peeks and the "file fix" form leave the list for the detail.

### 4.2 The detail, `/morpheus/{item_id}`
GET route reading `ops.morpheus_detail`; unknown id renders `error.html` with 404.
- Header: id, PR link and title, project, state (toned), release tag, the work order the
  PR names (linked), created/updated.
- Actions strip: **retry now** (POST existing `/morpheus/{id}/requeue`), **waive** with a
  reason, **confirm failed** with a fix text (only for failed/timedout not yet confirmed).
  Each form posts `next=/morpheus/<id>` so it lands back on the detail.
- Retries: checks run, auto-requeues used of max, next check due (`fmt_ts` and
  `fmt_left`), or why none is due (settled, held).
- Tabs (`.tabbed`): **runs** (table: time, kind, exit, state after, output in a fold),
  **last output** (`pre`), **check script** (`pre` + source agent/user + the PR's
  validation hints), **Neo questions** (each linked to `/neo/question/<id>`, status,
  answer headline), **handoff** (who it went to, linked: Neo question, investigation via
  `/go/<id>`, improvement order; the waiver or resolution text).
- Opening `/morpheus/<id>` from the list and from `/go/mv-…` both work (`ops.resolve_ref`
  must send `mv-` ids here).

## 5. Operations pages: stuck, steward, evolution

Owns `stuck.html`, `steward.html`, `evolution.html` and their routes.

- **/stuck**: primary = orders past their threshold, full width, as `needs_you` when any;
  then "on the record" as the full-width table. The explanatory paragraph goes in
  `ui.explain`. Filters: project, kind (wo/fo/io), status. Ids link to the existing order
  routes (`_order_href`).
- **/steward**: one primary table with tabs **waiting on you** / **waiting** /
  **re-offered and expired**; "waiting on you" also feeds `needs_you`. Subject is a
  `td.prose` clamp; the NOTE column leaves the table for a detail route
  `/steward/{wo_id}` that shows the decision record and its rule through the ops function
  behind `jarvis steward why <wo-id>`. Filters: project, tab state.
- **/evolution** and **/evolution/{project}**: primary = rule by rule, one row per detector
  with name, count, trend; the detector description and "never fired" text leave the
  table. Detectors that never fired fold into a closed "quiet detectors" section. Each rule
  links to `/evolution/rule/{key}` (optional `?project=`): description, firing history,
  what it filed, read from `ops.evolution_report`. Filters: project, days, firing/quiet.
  Sections that are a single empty-state line collapse to one line under the primary view.

## 6. Record lists: gates, backlog, knowledge, search

Owns `gates.html`, `backlog.html`, `knowledge.html`, `search.html`, their routes, and new
detail templates. Moves the backlog and knowledge routes' direct `CentralStore` reads
behind ops functions.

- **/gates**: `needs_you` = escalated; filters status, project, kind; primary = pending
  then decided, full width. New `/gates/{approval_id}`: the request text, why/evidence,
  Neo's review, decision, the approve/deny/dismiss form (existing POST
  `/gates/{id}/decide`), reading through the ops function behind `jarvis gate show`.
- **/backlog**: filters status, project; blocked items flagged. New `/backlog/{item_id}`:
  title, description, dependencies (linked), promote form (existing POST).
- **/knowledge**: filters topic (exists), project, pinned; headline clamp. New
  `/knowledge/{kn_id}`: full body, topic, origin order (linked), reads count, pin toggle
  (existing POST), via the ops function behind `jarvis learn show`.
- **/search**: results full width, kind/project filters through `ui.filter_bar`.
- `ops.resolve_ref` sends `gate-<n>`, `bl-…`, `kn-…` to these routes.

## 7. Order views: dashboard, project, orders

Owns `dashboard.html`, `project.html`, `feature_order.html`, `investigation_order.html`,
`improvement_order.html`, and only the top summary/asks layout of `work_order.html`.

- **Dashboard**: `needs_you` = the attention items (replaces the current attention block's
  position beside other panels); primary = open work full width with filters project and
  status; projects, new order and the brake move below or into tabs. The live-poll regions
  (`jarvisEnhance`) keep working.
- **Project**: work orders full width as primary with filters status; improvement,
  investigation and feature orders in tabs.
- **Feature / investigation / improvement order**: the summary as `page_head`; children,
  findings and asks full width; long briefs in `ui.explain`. Child rows use `row_id`.
- **Work order**: the summary card and the asks stack full width instead of side by side;
  the existing tabs are not changed.

## 8. Analytics views: alarms, neo, cost, bill, config

Owns `alarms.html`, `alarm.html`, `neo.html`, `neo_question.html`, `neo_stats.html`,
`cost.html`, `bill.html`, `config.html` and their routes.

- **/alarms**: `needs_you` = live alarms; review queue then history full width; history
  filters kind, project. Rows link to `/alarms/{project}/{id}` (exists).
- **/neo**: `needs_you` = escalated + failed; the existing tabs stay; history filterable by
  project and status; question ids link to `/neo/question/{id}`. **/neo/question/{id}** and
  **/neo/stats**: single full-width column.
- **/cost**, **/cost/{name}/{order}**, bill: the primary table full width, project filter;
  order ids link to their order pages.
- **/config**: the tree stays a sticky navigation column (it is navigation, not a squeezed
  panel); history and diff stack full width instead of side by side. Config has about ten
  browser tests; keep them green.

## 9. Not in scope

- The debug, prompt, spec and session pages (`debug.html`, `_debug*.html`, `prompt.html`,
  `project_prompt.html`, `spec.html`, `_sessions.html`): filed to the backlog as a
  follow-up pass.
- New data beyond §3. Other pages show only what their ops functions already return.
- Moving direct store reads in routes a section does not otherwise rewrite.
- Any change to CLI output except `jarvis morpheus show` (§3).

## Agent profile

You are a Jarvis OS engineer redesigning one part of the `jarvis ui` dashboard. Jarvis OS
is a stdlib-first Python package (`src/jarvis/`) that runs a fleet of Claude Code workers;
the dashboard is a FastAPI app (`src/jarvis/ui/app.py`, every route a closure inside
`create_app`) rendering Jinja templates in `src/jarvis/ui/templates/`, all extending
`base.html`.

Read first: the Serena memories `codebase-map` and `testing`, then learning kn-fa085e88
(`jarvis learn show kn-fa085e88`): the user's three UI rules — one full-width primary view,
attention first with query-string filters, every row id opens a detail route. Use Serena's
`find_symbol` / `find_referencing_symbols` / `get_symbols_overview` for Python; a hook
refuses text search over `.py` files.

Conventions:
- Layering runs downward: stores, then `ops`, then `cli`/`ui`. New UI code calls `ops`
  functions only, never `CentralStore`, `NeoStore`, `ProjectStore` or `sqlite3`. If the
  data you need has no ops reader, add one beside its domain's other readers in `ops.py`
  (never at the end of the 14k-line file) and have the CLI use the same one when it shows
  the same record.
- Use the shared pieces from §2 of the spec: `query_filters`, `filter_rows`,
  `filter_options`, `attention_first` in `app.py`, and the macros in `_layout.html`
  (`page_head`, `needs_you`, `filter_bar`, `row_id`, `explain`). Do not edit `base.html`
  (only the foundation section does) and do not add module-level helpers to `app.py`;
  page CSS goes in the template's `{% block style %}`. If a shared piece is missing
  something you need, add it to your page and file the gap with `jarvis backlog add`.
- Edit next to your own routes in `app.py`. In `tests_browser/test_ui_browser.py` add new
  test functions; do not reorder or rewrite other pages' tests.
- POST actions redirect with a `next` form field through `_same_site_back`; keep that.
- Bare ids in text autolink to `/go/<id>` via `base.html`; an id cell uses `ui.row_id` with
  the record's real detail route.
- Existing tabs are `.tabbed` containers with `.tabs` buttons and `.tabpanel`s; reuse them.

Proving it:
- Targeted tests only, never the full suite (`uv run pytest tests/test_ui.py -q`, plus the
  test files for any ops/store code you touch). Run `uv sync --extra dev` first in a fresh
  worktree.
- Browser tests: `uv run pytest tests_browser -q -k <your tests>`. Every page you change
  gets a test that loads it at 1440×900 and at 390×844 and calls the `readable` fixture
  (§2.6), plus a test that a filter in the URL narrows the rows, that a failed/needing-you
  row is above the others, and that clicking a row id navigates to its detail route (the
  URL changes; it does not just scroll).
- Fixtures: `jarvis_home` is autouse; the project fixture is `proj_a`; never call
  `monkeypatch.undo()`. A multi-row SQL fixture edit goes in `db.write_transaction(...)`
  (kn-fd19afc7).
- Take a screenshot of each page you changed at both widths and attach it to the PR (the
  `open-a-pull-request` skill says how).

Never: reintroduce a side-by-side grid of sections that compresses text; put a long prose
paragraph in a table column; make a click on an id scroll instead of navigate; query a
database from UI code; touch another section's templates.
