# Work the user asked for: stamped, reported back when done, and its own view

Feature order `fo-8786a1b6`, filed from decision order `do-5773e8c8` (option b).

## 1. Why, what is fixed, and the shared interfaces

### 1.1 The defect

`io-b42dfb75`, filed by the user, settled with no notice. Its 4 findings were Neo-accepted,
steward-dispatched, and 4 PRs merged; the user heard nothing. Three causes:

1. Only plain work orders (`ops.notify_completed`) and decision orders notify on settle.
   `Daemon._complete_feature`, the improvement-order settle and the investigation settle
   write no notice. An io reads `completed` once its findings are FILED, before the fixes
   run, so a notice there would come too early anyway.
2. `ops._file_finding_orders` creates fix orders with `parent_id=None` and a steward stamp.
   `ops.requestor` walks `parent_id` only, so they read as OS work.
3. User-vs-OS is re-derived three ways (`launched_by_os`, `ops.requestor`, `wo_tags`) and
   never stored, so nothing can filter on it. Every CLI create defaults `--origin jarvis`.

### 1.2 Fixed by the user (do not re-litigate)

- Autonomy is right. The gap is the report back, not a missing approval step.
- Requestor is decided ONCE, at creation, and stored. Readers read the column.
- One push notice per user ASK (root), when the whole tree is terminal. Children of a user
  root send no notice of their own.
- `launched_by_os` keeps its meaning. `notify_completed`, `park_user_action` and `wo_tags`
  callers change only where a section says so, with a test.
- Silence is worse than a bad answer (kn-53a20d3a, pinned): a tree that ends failed or
  cancelled still gets its notice.
- Out of scope: which ledger pays for orders filed from a user's improvement order
  (backlog `bl-a00454ee`).

### 1.3 Decisions this spec makes

1. An order a WORKER files (`JARVIS_WO_ID` set) with no parent and no `origin_io` is `os`.
   Inheriting from the filing order was rejected: such an order is outside the tree walk
   (§3), so it would become a second user root the user never typed.
2. Rows created before §2 ships keep `requestor` NULL. There is NO backfill. NULL rows are
   never roots for §3/§4/§6, so the first sweep cannot flood the inbox. Readers that must
   classify a NULL row (chips, filters) call `ops.requestor`, which keeps today's walk.
3. The notice is exactly once per root, keyed by event `request_settled`. A tree that
   settles failed and is later reopened (`wo retry`, `fo resume`) gets no second notice.
4. `waiting_pr_merge` and `needs_review` are NOT terminal. Terminal is
   `project_store.TERMINAL_STATUSES` for work orders and `FO_TERMINAL_STATUSES` for
   feature-table orders.

### 1.4 Shared interfaces

| Name | Shape | Owner |
|---|---|---|
| `work_orders.requestor`, `feature_orders.requestor` | `TEXT` `'user'`\|`'os'`, NULL = legacy | 2 |
| `ops.REQUESTOR_USER = "user"`, `ops.REQUESTOR_OS = "os"` | constants | 2 |
| `ops.interactive_requestor() -> str` | `'user'` when `JARVIS_WO_ID` is unset, else `'os'` | 2 |
| `ops.requestor(store, wo) -> Requestor` | unchanged signature; reads the column first | 2 |
| `ProjectStore.user_roots(include_settled=False) -> list[dict]` | stamped `user` rows of both tables with no parent and no `origin_io` | 3 |
| `ops.request_tree(project, root_id) -> RequestTree` | the root and every descendant, any project | 3 |
| `RequestTree` / `RequestNode` | dataclasses, §3.2 | 3 |
| event `request_settled` on the root | `{root_id, statuses: {id: status}, inbox_id}` | 4 |
| `Daemon.settle_request_trees()` | the sweep that sends the notice | 4 |
| `ops.completion_lines(store, wo) -> list[str]` | body lines factored out of `notify_completed` | 4 |
| `ops.request_state(project, root_id) -> str` | `needs_you`\|`running`\|`done_unseen`\|`done` | 6 |
| `requestor_seen_at REAL` on both tables; `ops.mark_request_seen(project, root_id)` | | 6 |
| `ops.is_user_requested(store, row) -> bool` | the one classifier every surface uses | 5 |
| query parameter `requestor=you\|os\|any` (default `any`) | dashboard, project page, inbox | 5 |
| `--mine` on `jarvis wo list`, `fo list`, `io list` | | 5 |

## 2. The requestor stamp

### 2.1 The column

`requestor TEXT` on `work_orders` and `feature_orders` via `ProjectStore.ADDED_COLUMNS`.
`ProjectStore.create_work_order` and `create_feature_order` (and every store method that
inserts a row in either table, e.g. `create_plan_children`, `create_manager_order`) take
`requestor: str | None = None`. Values other than `'user'`, `'os'`, `None` raise
`ValueError`. No raw SQL outside the store.

The store sits below `ops` in the layering and must not import it. So resolution (§2.2)
lives in `ops.resolve_requestor(store, *, parent_id, metadata, origin) -> str`, called by
the ops create functions and by every other create site. The only store-side rule: when
`requestor` is None and `parent_id` names a row whose column is set, copy that column (one
SELECT, no ops call); this covers `create_plan_children` and `create_manager_order`.

### 2.2 Resolution, in order, at every create

1. An explicit `requestor=` argument wins.
2. A `parent_id`: copy the parent's stamp (parent looked up in feature orders first, then
   work orders, as `ops.requestor` does). A NULL parent stamp resolves through
   `ops.requestor(parent).kind`.
3. `metadata["origin_io"]` (`ops.ORIGIN_IO_KEY`) or `metadata[ops.ORIGIN_DO_KEY]` (decision
   follow-ups, `ops.file_decision_followups`): copy that order's stamp. The io may live in
   a different project from the order (`_file_finding_orders` takes a `target`), so the
   caller passes `requestor=` explicitly from the io it already holds.
4. `origin` in `ops._SELF_ORIGINS`, or a steward stamp: `'os'`.
5. Otherwise `ops.interactive_requestor()`.

Rule 5 is why the CLI and UI need no flag: a person's terminal or browser has no
`JARVIS_WO_ID`. The UI create routes in `ui/app.py` pass `'user'` explicitly (the UI
process may inherit an environment). OS internals that create with `origin="jarvis"`
(e.g. `morpheus.py` investigations, `issues.promote_confirmed`, remedies, daemon-created
orders) pass `requestor='os'` explicitly: rule 5 must never be what classifies daemon work,
because the daemon has no `JARVIS_WO_ID` either.

User entry points that run in-process for a person and must stamp `'user'`: `jarvis wo|fo|io|do create`,
`jarvis investigate`, `ops.promote_backlog`, `issues.start_work` (`jarvis issues start`),
`jarvis mo confirm`'s filed order, the UI release order. Inherit sites: `_file_finding_orders`,
`file_decision_followups`, plan children and the manager order, the daemon's planner,
analyst, investigator and researcher rows. Find every remaining caller of the two store
create methods with `find_referencing_symbols` and classify each one; the PR lists them.

### 2.3 `ops.requestor`

Reads `row["requestor"]` first: `'user'` returns `Requestor("user", "you")`; `'os'`
returns the same OS label it returns today (steward stamp / origin label). Only a NULL row
falls through to today's `parent_id` walk, which additionally follows
`metadata.origin_io` (same project only; not found reads `os`, logged). Its labels are
read by `autoreview._requestor` and `Daemon._notify_owner_ruling`; keep them.

`launched_by_os` is NOT changed.

### 2.4 Done means

Tests assert the COLUMN (`get_work_order(id)["requestor"]`), not `ops.requestor`, which
already answers `user` for many cases through its walk. Tests that need `os` from rule 5
set `JARVIS_WO_ID` with `monkeypatch.setenv` (the `jarvis_home` fixture unsets it).
Existing tests that build plain dicts without a `requestor` key keep passing (`.get`).
Tests in `tests/test_requestor.py` (exists) cover: CLI-shaped create with no
`JARVIS_WO_ID` is `user`; same with `JARVIS_WO_ID` set is `os`; plan child of a user fo is
`user`; finding order of a user io is `user` even with a steward stamp, including a
cross-project target; `morpheus` investigation is `os`; steward-stamped root is `os`;
NULL legacy row still classified by the old walk plus `origin_io`. Existing requestor and
autoreview tests pass unchanged.

## 3. The request tree

### 3.1 Roots

`ProjectStore.user_roots(include_settled=False)`: rows of both tables with
`requestor='user'`, `parent_id IS NULL`, and neither `origin_io` nor `origin_do` in
metadata. A root is a work order, feature, improvement, investigation or decision order.
With `include_settled=False`, roots with a `request_settled` event are excluded (that event
is §4's; tests here write it by hand with `store.add_event`, name fixed in §1.4).

### 3.2 The walk

`ops.request_tree(project, root_id) -> RequestTree`.

```python
@dataclass
class RequestNode:
    id: str; project: str; kind: str; title: str; status: str
    terminal: bool; pr_url: str | None; needs_attention: bool
    children: list["RequestNode"]

@dataclass
class RequestTree:
    root: RequestNode
    settled: bool          # root and every descendant terminal
    def nodes(self) -> list[RequestNode]   # pre-order, root first
```

Edges, all followed recursively with a visited set:

- `parent_id` in both tables (every kind: planner, manager, researcher and workers count).
- An improvement order's `report.findings[*].created_orders[*]` (records the target
  project), and as a fallback any row in the same project whose `metadata.origin_io`
  is the io.
- A decision order's filed follow-ups: rows whose `metadata[ORIGIN_DO_KEY]` is the do.
  The do's stored `filed` marks (`{"id","as","at"}`) carry no project, so this section
  adds `"project"` to each new mark in `ops.file_decision_followups` and follows it; a
  legacy mark without one is looked up in the do's own project only.

A child in another project is read through that project's store. A missing child is a
node with `status="missing"`, `terminal=True`, logged; it never blocks settlement forever.

### 3.3 Done means

New `tests/test_request_tree.py`. Terminal is `project_store.TERMINAL_STATUSES`
(includes `failed`), never `invariants.TERMINAL_STATUSES`. Tests: a missing child is
`status=="missing"`, terminal; a user do's follow-up is in the do's tree and not a root; a plain user wo with no children is a one-node settled tree; a user fo with
planner + 2 children settles only when all are terminal; a user io whose finding orders
run in another project is unsettled while one runs and settled after; `waiting_pr_merge`
is not terminal; a cycle terminates; `user_roots` excludes children, finding orders, `os`
rows and NULL rows.

## 4. The tree-settled notice

### 4.1 Trigger

`Daemon.settle_request_trees()` runs on the daemon's existing periodic pass (where
`settle_review_pings` and the like run). For each project and each `user_roots()` row
whose own status is terminal: build `request_tree`; if `settled`, then inside one
`db.write_transaction` re-check that no `request_settled` event exists for the root, add
the notification, then the event. A sweep, not hooks at settle sites: no single settle site
sees the LAST descendant land (an io completes before its fixes run).

A failure building the body is logged and the root is retried next pass; the OS never
writes the event without the notice (kn-40db1828).

A decision order already notifies when its report is delivered (`raise_decision`,
`source="decision"`), and only the user closes it. So a do root with NO descendants writes
the event with no notice (the user closed it themselves; telling them is noise). A do root
with follow-ups gets the tree notice once they are all terminal.

### 4.2 The notice

`store.add_notification(level=..., wo_id=root_id, source="request")`, the same path
`notify_completed` uses, so it pushes the same way.

- Title: `<root-id> done: <root title>`.
- Body, in order:
  1. Outcome: one line. `completed`, or `ended <status>` naming each non-completed node.
     For a work-order root, the lines from `ops.completion_lines` (summary, owed user
     action, final-message tail). For an io/do/investigation root, its report or verdict
     summary line.
  2. `Orders run:` one line per non-root node: `<id> <status> <title>`.
  3. `Decided for you:` each assumption on any node decided by Neo or the OS (not the
     user), and each steward decision recorded on a node's stamp (`decision #n`). Omit the
     block when empty.
  4. `Pull requests:` every node's `pr_url`.
  5. `Cost: $x.xx`: sum over work-order nodes of the per-order cost `jarvis cost <wo-id>`
     reports (`ops.cost_report(target=...)`).
  6. Deep link: `<base>/yours#<root-id>`, base from `notify.ui_base_url(catalog)`, never a
     literal host.
- Level `warning` when any node ended `failed` or an owed user action exists, else `info`.

### 4.3 No other notice for these trees

`ops.notify_completed` returns None for any row whose `requestor` column is not NULL: a
stamped user root is told by §4.2, a stamped child is never told separately, and `os` rows
were never told. NULL (legacy) rows keep today's behaviour exactly. `notify_completed`'s
body is factored into `ops.completion_lines(store, wo)` so §4.2 reuses it.

### 4.4 Done means

New `tests/test_request_settled.py`, using the started Daemon fixture pattern of
`tests/test_inbox_attention_pairing.py`, calling `settle_request_trees()` repeatedly and
counting notifications with `source=="request"`. Cost reads `$0.00` in tests (no
transcripts); assert the line or monkeypatch `ops.cost_report`. Deliberately rewrite
`tests/test_work_lands.py::test_a_user_order_that_merges_tells_the_user_with_its_final_message`:
a stamped user order now gets the tree notice, not `notify_completed`'s. Tests: when
`completion_lines` raises, no event and no notice are written; a user do with no
follow-ups writes the event and no notice; a user fo tree notifies exactly once after its last child terminates, across
repeated sweeps; no notice while a child is `waiting_pr_merge`; a child's completion
writes no notification; a plain user wo root gets exactly one notice (the tree notice,
carrying its final-message tail), not two; a NULL legacy wo still gets today's
`notify_completed` notice; an `os` root gets none; a failed tree gets a `warning` notice;
title and every body block are asserted on one fixture. `timeline._describe` labels
`request_settled`.

## 5. Filters, chips and `--mine`

### 5.1 One classifier

`ops.is_user_requested(store, row) -> bool`: the column when set, else
`ops.requestor(store, row).kind == "user"`. Every surface below uses it; none reads
`launched_by_os` for this purpose.

Rows handed to the dashboard, project page and inbox carry a derived key
`requestor_view` (`you`|`os`) computed with it; the filter matches that key, never the raw
column (`filter_rows` matches values exactly, and the raw column says `user`). `any` is
dropped before `query_filters`, which otherwise keeps an unknown value "to match nothing".

### 5.2 Surfaces

- Dashboard (`ui/app.py` dashboard route, `dashboard.html`) and project page
  (`project.html`): a `requestor` filter `you|os|any`, default `any`, built on the existing
  generic filter machinery (`filter_options` / `query_filters` / `filter_rows`), so it
  composes with type/project/status and survives in the query string.
- Inbox (`inbox.html` and its route): same filter. A row's requestor is its `wo_id`'s
  order's; a row with no `wo_id`, or whose order is not found, is `os`. The route's
  `filtered` flag counts `requestor`, so "Ack shown" acks only the rows shown.
- Row chips: `ops.wo_tags` shows `you` or `os` instead of `manual`/`automatic`. It takes no
  store and templates call it directly (`work_order.html`, `project.html`), so it reads the
  `requestor` column (or `requestor_view` when present) and falls back to
  `launched_by_os` only for a NULL row. Its pins in `tests/test_issue_lifecycle.py`
  change deliberately, in this section's PR.
  `park_user_action` keeps `launched_by_os`.
- CLI: `jarvis wo list --mine`, `jarvis fo list --mine`, `jarvis io list --mine` keep only
  user-requested rows. The CLI handler calls an `ops` list function with `mine=True`; no
  logic in `cli.py`. The CLI list chip reads `[you]`/`[os]` the same way.

### 5.3 Done means

Tests: each filter value on dashboard, project page and inbox (TestClient), `any` is
today's output, chips read `you` for a child of a user fo, `--mine` on each list verb.

## 6. The Yours page and seen state

### 6.1 Seen

`requestor_seen_at REAL` on both tables (`ADDED_COLUMNS`). `ops.mark_request_seen(project,
root_id)` sets it to now. Called by: a GET of the root's own order page (work order,
feature, improvement, investigation or decision page), and `ops.ack_attention` for a
work-order root (so `jarvis wo ack <wo-id>` marks seen; `ack_attention` resolves work
orders only, and widening it is out of scope). Seen is only meaningful once the tree
settled; marking earlier is harmless. The `request_settled` event is §4's: tests here
write it by hand with `store.add_event`.

### 6.2 State, one per root, first match wins

1. `needs_you`: any node has `needs_attention`, pending user assumptions, or an owed user
   action (`declared_user_action`).
2. `running`: tree not settled.
3. `done_unseen`: settled and `requestor_seen_at` is NULL or older than the
   `request_settled` event.
4. `done`.

`ops.request_state(project, root_id)` returns it.

### 6.3 The page

Route `/yours` (new template `yours.html`, nav link in `base.html`). One section per state
in the order above; each root a row with id, project, kind, title, state, cost to date,
and its tree (`request_tree`) collapsed beneath, each node linking to its own page. `done`
roots older than 14 days are behind a "show older" link. Project filter as on the
dashboard. Uses `user_roots(include_settled=True)` across projects; it does not touch the
§5 filters.

### 6.4 Done means

Tests: each state on a fixture tree; viewing a settled root moves it `done_unseen` to
`done`; `jarvis wo ack <root-id>` does the same; the page renders each state's section and
a cross-project child (TestClient).

## 7. Out of scope

- Which ledger pays for finding orders (`bl-a00454ee`).
- Backfilling `requestor` on existing rows.
- A first-class "request" entity (option c of `do-5773e8c8`).
- Changing `launched_by_os` or the owner-mode assumption review (fo-eaf1a59a).
- A second notice when a reopened tree settles again (§1.3 decision 3).

## Agent profile

You are the REQUESTOR ENGINEER, building one section of a Jarvis OS feature in the dev
checkout: every order records whether a person asked for it, the user is told once when
their whole ask has settled, and a "Yours" view shows only their work. You write Python
in `src/jarvis/`, Jinja templates in `src/jarvis/ui/templates/`, and tests in `tests/`.

The rule above every other: **the stamp is decided once, at creation, and read
everywhere.** Read `requestor` from the column; fall back to `ops.requestor` only for a NULL
(legacy) row. Never re-derive user-vs-OS from `origin` or `launched_by_os`: `origin`
defaults to `jarvis` for user and OS creates alike, which is the defect this feature fixes.

The second rule: **never be silent, never fabricate** (kn-53a20d3a, kn-40db1828, both
pinned). A failed or cancelled tree still gets its notice. A failure building a notice
leaves the root unsettled and retried; never write the `request_settled` event without the
notification.

What you must know about this codebase:

- **The CLI is the OS.** Never touch SQLite directly outside the store classes. Reads and
  writes go through `ProjectStore` / `CentralStore` methods; a user action is an `ops.py`
  function that `cli.py` or `ui/app.py` calls. A CLI handler holding logic is a defect. New
  columns go in `ProjectStore.ADDED_COLUMNS`.
- **Feature, improvement, investigation and decision orders all live in `feature_orders`**
  (distinguished by `kind`); work orders in `work_orders`. Ids are unique across both.
  `parent_id` on a work order may point at either table.
- **Terminal** is `project_store.TERMINAL_STATUSES` (work orders) and
  `FO_TERMINAL_STATUSES` (feature table). `waiting_pr_merge` and `needs_review` are not
  terminal. Settlement code re-runs every daemon tick (kn-99d3f1d4): anything you add to the
  daemon must be idempotent and proven by a test that ticks repeatedly.
- **Navigate with Serena** (`find_symbol`, `find_referencing_symbols`,
  `get_symbols_overview`) after reading the memories `codebase-map`,
  `work-order-lifecycle`, `feature-orders` and `testing`. Grep only for text. Read this
  spec with `jarvis spec toc` / `jarvis spec section`; use the names in §1.4 exactly, a
  sibling relies on them.

Conventions:

- House style (caveman full) for every word a person reads: notice text, chips, commit
  messages, PR bodies. Exact strings, ids, numbers and not/never/only are never compressed.
- Comments carry the one-line WHY.
- Every new timeline event gets a label in `timeline._describe`.
- The PR body carries `## How to validate in production` (CI checks it).
- Tests use the fixtures in `src/jarvis/testing.py` (`jarvis_home`, `fake_claude`,
  `project`); none call the real `claude` or the network. UI tests use FastAPI's
  TestClient as the existing `tests/test_ui*.py` do. Run ONLY the test files covering what
  you changed (`uv sync --extra dev`, then `uv run pytest tests/<file> -q`); the full suite
  is CI's job (kn-356c724b). Grep `tests/` for strings you change: exact pins elsewhere go
  red.

Traps:

- Do not change `ops.launched_by_os`. `park_user_action` and others rely on it.
- The daemon has no `JARVIS_WO_ID`, so `ops.interactive_requestor()` would call daemon work
  `user`. Every OS-internal create passes `requestor='os'` or inherits; only real user
  entry points reach rule 5 of §2.2.
- Finding orders can be filed into a different project from their io; pass the stamp
  explicitly, do not look the io up in the target project.
- Do not backfill old rows, and never treat a NULL row as a root: it would flood the inbox.
- Do not add a config knob, an opt-out or a second notice channel; none was asked.
- Do not build another section's piece because it looks small; your work order's
  description names who owns it.
