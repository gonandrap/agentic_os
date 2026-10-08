# A window selector for `/cost` and `jarvis cost --fleet`

Work order wo-4b163afc. Extends
docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md. Build order, not an essay.
Deterministic, read-only, no model call.

## The problem

The cost surfaces can only report ONE window, and the two halves of the `/cost` page do
not report the same one.

1. **The window is fixed to the current usage week and cannot be changed from the
   dashboard.** `fleetcost.window_of` (src/jarvis/fleetcost.py:121-131) takes `since` /
   `until` or falls back to `usage_week` (fleetcost.py:99-118). The only caller that can
   pass them is the CLI (`cli.cmd_cost`, src/jarvis/cli.py:2910-2912, flags declared at
   cli.py:548-551). The dashboard route calls `ops.fleet_cost(project=project or None)`
   with no window at all (src/jarvis/ui/app.py:1409), so `/cost` can report the current
   week and nothing else. "Did last week get dearer" — the question the distribution was
   built to answer (that spec's §1) — is unanswerable on the page that renders it.
2. **There is no 5-hour window anywhere.** Spend is rate-limited in 5h sessions; nothing
   in `fleetcost`, `catalog.CostConfig` (src/jarvis/catalog.py:989-1010) or the CLI names
   a sub-week period. The grid the user actually gets throttled on is unreportable.
3. **The page's two halves measure different populations, and neither says so.**
   `ops.cost_report` (src/jarvis/ops.py:12201-12261) has NO window: it walks
   `store.list_work_orders(limit=limit, include_hidden=include_hidden)` —
   `ORDER BY created_at DESC LIMIT 50` (project_store.py:2152-2167) — and sums every
   surviving transcript and the whole `agent_calls` table via `_os_groups`
   (ops.py:12264-12277), which calls `agent_call_totals(project)` with no `ts` clause.
   `ops.fleet_cost` right beside it reports one usage week. So the headline
   `total ~$…` and the per-order listing are all-time while the distribution panel below
   them (`_fleet_distribution.html:17`, "… (usage week)") is one week. The same page shows
   two scopes and labels only one.
4. **Nothing about a selection could survive a reload or be shared.** `cost_page`'s only
   query parameter is `project` (app.py:1393); the project links in `cost.html:10-13` are
   the whole state the URL carries.

Root cause of 1-3: the window was introduced as a REPORT argument (`report(since=,
until=)`) rather than as page state, so it reached exactly one of the two payloads the
page is built from, and only from a terminal. Root cause of 4 is the same thing seen from
the URL: the resolved window is an output of `fleetcost` and never an input to the route.

What this spec does NOT repair: `agent_calls` still has no turn id, so jarvis-side spend
is attributed to a turn by timestamp interval (that spec's §9). Narrowing the window makes
that attribution finer-grained, not sounder. Named here so the improvement is not mistaken
for a fix.

## The fix

One window resolver in `fleetcost`, called ONCE per surface, whose resolved output is
passed to BOTH payload builders. `ops.cost_report` gains the window and only filters with
it; it never computes a boundary. Default behaviour — no parameters anywhere — stays the
current usage week for the distribution, and becomes the current usage week for the
listing too, which is the one behaviour change and is the point of defect 3.

Decided before this spec and not re-opened here:

* **Neo q1420: every number on the page moves with the selection**, not only the
  distribution section, and `fleetcost` owns the boundary arithmetic and exports
  `since`/`until` to `cost_report`, which only filters. Exactly one window-resolution
  function exists.
* **No module constant for anything tunable** (Neo's standing rider on this feature). The
  5h length is a `CostConfig` field.

### 1. Where the resolution lives

`src/jarvis/fleetcost.py`, extending the existing window block (fleetcost.py:96-147).
Not a new module: `usage_week` is already there, is already the week-boundary primitive,
and already does the local-wall-clock arithmetic a weekly offset needs. Not `catalog.py`
(it parses settings, it does not compute with them), not `ops.py` (it must not grow a
second place that knows what a usage week is), not the UI route (the CLI needs the same
answer, and a resolver in a route is a resolver with one caller).

```python
WEEK = "week"                       #: the Claude usage week
SESSION = "5h"                      #: the 5-hour grid, anchored on the weekly reset
WINDOW_NAMES = (WEEK, SESSION)

def resolve_window(*, window: str | None = None, offset: int = 0,
                   since: float | str | None = None,
                   until: float | str | None = None,
                   cfg: CostConfig, now: float | None = None) -> dict[str, Any]
```

Returns, and these seven keys are the contract every surface reads:

| key | value |
|---|---|
| `since` | float epoch, INCLUSIVE |
| `until` | float epoch, EXCLUSIVE (half-open, like every other window in the OS) |
| `label` | the existing UTC label from `_label` (fleetcost.py:144-147), unchanged |
| `local_label` | the same span in `cfg.week_reset_zone` — see §4 |
| `source` | `"usage-week"` \| `"week-offset"` \| `"session-window"` \| `"flags"` |
| `window` | `"week"` \| `"5h"` \| `None` (custom) |
| `offset` | int, `0` or negative; `None` for custom |

`source` keeps the literal `"usage-week"` for `window="week", offset=0` so the existing
template branch (`_fleet_distribution.html:17`) and the existing CLI line
(`cli._print_fleet`, cli.py:3119-3120) keep reading without a change of meaning, and
`tests/test_ui_cost.py:479` (`assert "usage week" in page.text`) keeps passing.

**`window_of` stays, as a two-argument shim that delegates.** Its signature is public:
`fleetcost.report` calls it (fleetcost.py:986) and tests use it. It becomes

```python
def window_of(since, until, cfg, *, now=None) -> dict[str, Any]:
    return resolve_window(since=since, until=until,
                          window=None if (since is not None or until is not None) else WEEK,
                          cfg=cfg, now=now)
```

so there is still exactly one implementation and no caller is broken.

**`usage_week` gains `offset: int = 0`** and keeps its `(since, until)` tuple return. It
stays the week-boundary primitive; `resolve_window` is the only thing that interprets a
window NAME.

### 2. The 5h grid and its anchor

```python
def session_window(now: float, cfg: CostConfig, offset: int = 0) -> tuple[float, float]
```

```
week_start, _ = usage_week(now, cfg)          # offset 0: the CURRENT week's reset
length      = cfg.session_window_hours * 3600
k           = floor((now - week_start) / length)
since       = week_start + (k + offset) * length
until       = since + length
```

**Recorded assumption, and it must be stated in the payload's `notes` and in `--help`:
nothing in the codebase or in the usage data records Claude's real 5h session boundary.**
`claude_cli` parses the CLI's reset *sentence* (src/jarvis/claude_cli.py:1198-1326), which
is a limit message and not a grid; no table has a session id or a session start. So the
grid is anchored on the one reset instant the OS does know — the weekly one — and a 5h
window here is a 5h SLICE OF THE WEEK, not a claim about Anthropic's session accounting.
A reader must not be allowed to believe otherwise, which is why the sentence travels in
the payload rather than in one renderer.

Consequence, accepted deliberately: stepping back is CONTINUOUS IN ABSOLUTE TIME —
`offset=-n` is exactly `n * length` earlier — so a 5h window several weeks back may not
align with that older week's own reset (a week is 7 local days, which is 168h or 167h or
169h across a DST change, and 168/5 is not an integer). The alternative, re-anchoring the
grid on whichever week the target falls in, is rejected: it makes `offset` non-monotonic
and produces overlapping or gapped windows at every week boundary, so two adjacent offsets
could double-count or lose a turn.

### 3. Refusals, with their exact messages

Raised by `resolve_window` as `OpsError` (imported lazily, as `fleetcost.report` already
does at fleetcost.py:1002). One vocabulary, three surfaces: the CLI prints it, the route
renders `error.html` with it, `--json` fails rather than answering a different question.

```
window must be one of week, 5h — {name!r} is not a window this report knows
offset must be 0 or negative — {offset} names a window that has not happened yet
since must be before until — {since_label} is at or after {until_label}, which is an empty window
--since/--until and --window/--offset are two ways to name the same thing — pass one or the other, not both
```

The last one is checked before anything is computed; on the dashboard the same refusal
covers `?window=5h&since=…`. **A bad parameter is a refusal, never a silent fallback to
the default week**: a page that ignored `?window=5x` would report the week while the reader
believes they picked 5h, which is the one failure mode a selector must not have.

`offset` is an `int`; a non-numeric `?offset=abc` is caught by the route (§6) and comes
back as the OS's own sentence, not FastAPI's 422.

### 4. Weekly offsets, DST and the local label

Weekly offsets are computed on the LOCAL WALL CLOCK, the way `usage_week` already does and
for the reason its docstring already gives: subtract `7 * n` days from the NAIVE local
start, then convert once.

```python
start = local.replace(hour=cfg.week_reset_hour, minute=0, second=0, microsecond=0)
start -= timedelta(days=(start.weekday() - cfg.week_reset_weekday) % 7)
if start > local:
    start -= timedelta(days=7)
start += timedelta(days=7 * offset)             # offset <= 0
return (start.replace(tzinfo=zone).timestamp(),
        (start + timedelta(days=7)).replace(tzinfo=zone).timestamp())
```

A window spanning a DST change is therefore still seven LOCAL days starting at the
configured hour, and is 167 or 169 absolute hours long. Absolute arithmetic
(`now - 7*86400*n`) would slide the reset by an hour for every crossing and is rejected.
`replace(tzinfo=…)` on a naive local time resolves an ambiguous hour with `fold=0`
(PEP 495), which is the behaviour the current code already has; this spec does not change
it and the test matrix in §8 pins it.

`local_label` renders the same span in `cfg.week_reset_zone` with `%Z`:
`2026-09-28 21:00 to 2026-10-05 21:00 PDT`. **When the two ends have different `tzname()`
values, print both** (`… 2026-11-02 21:00 PST`) — a single abbreviation across a transition
is a label that is wrong at one end. Both labels go on the page: the UTC one is what
`agent_calls.ts` is comparable to, the local one is the clock the reset is specified in.

### 5. `ops.cost_report` filters, and only filters

New keyword only: `cost_report(project=None, target=None, limit=50,
include_hidden=True, window: dict[str, Any] | None = None)`. `window` is the dict
`resolve_window` returned. `None` keeps today's whole-history behaviour exactly, so
`_cost_for_target` (a single order's bill) is untouched — **a window is never applied to
`target=`**: one order's bill is the whole order, and truncating it would make the bill
stop reconciling, which `bill.py`'s contract forbids.

Three filters, one per source of figures:

**(a) Which work orders.** An order is in the window if it has a `wo_turns` row with
`started_at` in `[since, until)`, or an `agent_calls` row in the window. Filtering TURNS
and not `created_at` is the ruling the parent spec already carries (§3, §8).
`list_work_orders` gains `active_between: tuple[float, float] | None = None`, adding

```sql
AND EXISTS (SELECT 1 FROM wo_turns t WHERE t.wo_id = work_orders.id
            AND t.started_at >= ? AND t.started_at < ?)
```

**THE WINDOW MUST BE APPLIED BEFORE `LIMIT`, not after.** The query is
`ORDER BY created_at DESC LIMIT ?` (project_store.py:2164); filtering in Python after the
fetch would return an EMPTY last week as soon as a project has 50 newer orders — a wrong
answer that looks like a fact about the fleet. Orders that have in-window `agent_calls` but
no in-window turn (Neo answered a question between turns) are added by id afterwards, from
the keys of the windowed `_os_groups`.

**THOSE IDS MUST BE NARROWED TO THE OPEN PROJECT FIRST.** `_os_groups` is read
fleet-wide, the report opens one `ProjectStore` at a time, and
`ProjectStore.get_work_order` RAISES `KeyError` on an id its database does not hold
(project_store.py:1932-1936) — so an unnarrowed pass makes a fleet-wide `/cost` 500 as
soon as two registered projects both have in-window `agent_calls`. `agent_calls.project`
is the filter (`_os_only_ids`); a row that names no project is a candidate everywhere and
the lookup itself tolerates the `KeyError`.

**(b) What Jarvis spent.** `_os_groups(project, since=None, until=None)` passes both
through to `CentralStore.agent_call_totals`, which already supports them, half-open
(central_store.py:1856-1868). The only SQL change is `project` joining the SELECT and the
GROUP BY, so (a) can place an id without a lookup; every consumer re-aggregates, so the
finer key changes no figure.

**(c) The transcript-derived figures.** `usage.read_session` and `usage._usage_of` gain an
optional half-open window:

```python
def read_session(session_id, cold_prefix_floor, root=None, index=None,
                 *, since: float | None = None, until: float | None = None) -> SessionUsage
def _usage_of(path, cold_prefix_floor, *, since=None, until=None) -> Usage
```

Additive and defaulted: the other two callers — `ops._unit_row` (ops.py:12112, which
passes them through) and `bill._wo_usage` (bill.py:1184, which must NOT) — are unaffected
unless they ask. Inside `_usage_of`, filtering happens on the DEDUPED message list
(`_assistant_messages`, usage.py:586-631), whose `ts` is `parse_stamp` of the FIRST copy of
the message, i.e. when the call landed.

* **Token sums, cost, `context_peak` and `rewrite_excess` come from the in-window messages
  only.**
* **Boundaries are classified over ALL the file's calls and then filtered by
  `Boundary.ts`.** Not the other way round: `classify_boundaries` (usage.py:742-793) decides
  "the cache went backwards" by comparing a call with its PREDECESSOR, so filtering the
  calls first would silently lose the boundary at the window's left edge — and that is the
  interesting one, since the first turn of a window is usually a cold start. This is also
  what the existing note already says, that boundary causes are a property of the
  conversation and the transcript carries no window.
* **A message whose timestamp cannot be parsed — `parse_stamp` returns `0.0`
  (usage.py:575-583) for a missing or malformed `timestamp` — CANNOT BE PLACED. When a
  window is given it is EXCLUDED AND COUNTED, never silently dropped and never counted as
  in-window.** New field `Usage.undated_messages: int`, summed in `Usage.__add__`
  (usage.py:260-294) and emitted by `Usage.as_dict` (usage.py:355-380), which is spread
  into every row by `_unit_row` (`**total.as_dict()`, ops.py:12150) and so into
  `cost_report`'s `units`, `totals` and `--json` with no further plumbing. With no window
  it is always `0`: nothing is excluded, so there is nothing to disclose. Where it
  surfaces: the CLI footer prints one line when non-zero
  (`N transcript messages carried no readable timestamp and are excluded from this
  window`), `cost.html` prints the same sentence under the headline, and `--json` carries
  the integer. Silence when zero.

`cost_report`'s payload gains one additive key, `window` — the resolved dict, or `null`
when no window was given — so a consumer can tell which population the existing
`measured` / `unmeasured` / `totals` keys describe.

### 6. Surfaces

**`fleetcost.report`** gains `window: str | None = None`, `offset: int = 0` and
`resolved: dict | None = None`. `resolved` wins and is REFUSED alongside any of
`since`/`until`/`window`/`offset`, so there is never a question of which one was used.
`report` resolves only when `resolved` is absent. The payload's `fleet.window` becomes the
resolved dict verbatim.

**`ops.cost_window(**raw) -> dict`** — a four-line lazy wrapper over
`fleetcost.resolve_window`, exactly like `ops.fleet_cost` (ops.py:12183-12198), so neither
the CLI nor the route imports `fleetcost` and the `ProjectStore`-free import graph that
module depends on stays intact.

**The route.** `/cost` gains four parameters, all strings so that every refusal is the OS's
sentence rather than a framework 422:

```python
def cost_page(request: Request, project: str = "", window: str = "",
              offset: str = "", since: str = "", until: str = "")
```

Resolution happens ONCE, inside the existing `try` that already renders `error.html` on
`ops.OpsError` (app.py:1401-1404), and the resolved dict is passed to BOTH
`ops.cost_report(project=…, window=resolved)` and `ops.fleet_cost(resolved=resolved)`.
That is what makes the two halves of the page incapable of disagreeing — the alternative,
passing the raw parameters to each and letting each resolve, re-runs a function of `now`
twice and can straddle a boundary between the two calls.

The resolved window is passed to the template as its own variable, NOT read out of
`fleet`: the distribution section is still built in a `try` that may set `fleet = None`
(app.py:1405-1411), and the selector must survive losing it. Losing the section must not
lose the window the reader picked.

**`cost.html`** gains a selector row beside the existing project links (cost.html:8-15):

* week: `‹ previous` (`?window=week&offset={offset-1}`), `current`
  (`?window=week&offset=0`); `next` is rendered ONLY when `offset < 0`, because
  `offset > 0` is refused.
* 5h: the same three, `window=5h`.
* custom: a plain `<form method="get" action="/cost">` with two
  `<input type="datetime-local">` named `since` and `until`, plus a hidden `project`.
  **Labelled UTC on the page**, because `compaction_payoff.parse_when` reads a naive stamp
  as UTC and the CLI's `--since` already behaves that way (cli.py:548-551); interpreting
  the form in `week_reset_zone` instead would make the same string mean two things on two
  surfaces. The implementer must confirm `parse_when` accepts the seconds-less
  `2026-10-01T13:45` that `datetime-local` submits, and pin it with a test.
* every link carries the current `project` so the two selectors compose.
* the active window's `local_label` and `label` are both printed, above the headline, where
  they describe the WHOLE page and not just the distribution panel.

No JavaScript: links and a GET form, the same way `cost.html` already does its project
switch, so the state is in the URL and is shareable by construction.

**CLI.** `jarvis cost --fleet` gains `--window {week,5h}` and `--offset N` beside the
existing `--since`/`--until` (declared at cli.py:544-554). `cmd_cost` (cli.py:2905-2917)
passes all four to `ops.fleet_cost`; `_print_fleet` prints `local_label` beside the
existing `label` line (cli.py:3119-3120). The listing branch of `cmd_cost` is NOT given a
window: `--fleet` is the windowed surface on the CLI, the page is the windowed surface in
the dashboard, and making the bare `jarvis cost` listing default to one week would change
the meaning of a command whose output people compare across months. Said explicitly
because it is an asymmetry a reviewer will ask about: on the page the two halves must agree
because they are side by side; in the terminal they are two separate commands.

### 7. Settings and payload version

One new `CostConfig` field (src/jarvis/catalog.py:989-1010), with its default beside the
others at catalog.py:977-986:

```python
DEFAULT_COST_SESSION_WINDOW_HOURS = 5.0
session_window_hours: float = DEFAULT_COST_SESSION_WINDOW_HOURS
```

Parsed in `_parse_cost` (catalog.py:1940-1996) against `base.session_window_hours` like
every other field, and refused with its own sentence rather than the count rule — a
fractional length is a legal belief about the grid, so the test is strictly positive:

```
{where}.session_window_hours must be > 0 — {value} is not a length of time
```

`jarvis config set <project> cost.session_window_hours 5` needs nothing further: the dotted
path is reflective (`config_version.resolve`, src/jarvis/config_version.py:130-143) and
`("*.cost.*", "hot")` is already in `APPLY_RULES`.

**`fleetcost.PAYLOAD_VERSION` stays 1.** Everything here is additive: `fleet.window` gains
`local_label`, `window` and `offset`; `cost_report` gains `window`; `Usage.as_dict` gains
`undated_messages`. `window.source` gains two new VALUES — a new value in an existing enum
is neither a removal nor a re-meaning of a key, and both existing readers already have an
`else` branch (`_fleet_distribution.html:17`, `_print_fleet`'s format string). The one
change that is NOT additive in spirit, and must be disclosed rather than versioned away:
**`cost_report`'s existing `measured` / `unmeasured` / `totals` keys describe a different
POPULATION when a window is given.** The keys are unchanged, which is why the version is
not bumped; the new `window` key is how a consumer tells, and it is `null` on every call
that does not pass one.

### 8. Tests the implementer writes

**`tests/test_fleetcost.py`** — synthetic, frozen `now`, no real state. Use the existing
`fleet_fixture` (src/jarvis/testing.py:2952-2980) for anything that needs a catalog;
`resolve_window` itself needs only a `CostConfig`, so test it directly, and use
`FleetCostFixture.set_cost(...)` (testing.py:2878) for `session_window_hours`.

1. `test_week_offsets_step_back_whole_local_weeks` — `offset` 0, -1, -4 from a frozen
   `now`; each span is seven local days, each `since` is the configured hour in
   `week_reset_zone`, and the windows abut exactly (`w[-1].until == w[0].since`).
2. `test_week_across_dst_in_both_directions` — a `now` in the week containing the US
   autumn transition (clocks back: the window is 169 absolute hours) and one containing the
   spring transition (clocks forward: 167 hours). In both, the local start is still the
   configured weekday and hour, and `local_label`'s two ends carry DIFFERENT `%Z`
   abbreviations, so both are printed. Repeat with `offset=-1` so the OFFSET path is the
   thing crossing the transition, not just the current week.
3. `test_week_boundary_second_either_side` — the existing boundary assertion
   (`test_usage_week_boundaries`) extended: one second before the reset with `offset=0` is
   the previous week, and the same instant with `offset=-1` is the week before THAT.
4. `test_session_window_grid` — a `now` 12h after the reset gives
   `[week_start + 10h, week_start + 15h)`; `offset=-1` is the preceding 5h; `offset=-3`
   is exactly `15h` earlier than the current `since`; a non-default
   `session_window_hours` (e.g. 1.0, and a fractional 2.5) moves every edge.
5. `test_session_window_steps_across_a_week_start` — enough negative offset to cross the
   reset; the step stays exactly `length` seconds and windows neither overlap nor gap.
   Asserts the accepted non-alignment of §2 rather than pretending it away.
6. `test_custom_range` — `since`/`until` as a date and as an ISO datetime (including the
   seconds-less `datetime-local` form); `source == "flags"`, `window is None`,
   `offset is None`; naive input is read as UTC.
7. `test_refusals` — one case per message in §3: `since >= until` (and `since == until`),
   `offset=1`, `window="month"`, `--since` together with `--window`. Each asserts the
   message TEXT, since three surfaces print it.
8. `test_window_of_still_resolves_the_week` — the shim's old two-argument behaviour and
   `source == "usage-week"`, so the public signature `fleetcost.report` depends on is
   pinned.
9. `test_payload_window_keys_stable` — `fleet.window`'s key set against a literal, and
   `PAYLOAD_VERSION == 1`.

**`tests/test_cost_report.py`** (where the attribution is already covered):

10. `test_window_filters_orders_before_the_limit` — 60 orders newer than the window plus
    one with a turn inside it, `limit=50`: the in-window order is present. The regression
    guard for the §5(a) trap.
11. `test_window_filters_os_calls_and_transcripts` — `agent_calls` either side of the
    window and a transcript with messages either side: the row's token figures and
    `os_cost_usd` count only the in-window ones; an order whose only in-window activity is
    an `agent_call` is still listed.
12. `test_undated_transcript_message_is_excluded_and_counted` — a transcript row with a
    missing and a malformed `timestamp`: `undated_messages == 2`, its tokens are in NO
    total, and the sentence appears in the CLI output. Same fixture with no window:
    `undated_messages == 0` and every message counted.
13. `test_a_targets_bill_ignores_the_window` — `cost_report(target="wo-…", window=…)`
    returns the whole order.
14. `test_a_windowed_fleet_report_spans_two_projects` — TWO registered projects, each with
    an order whose only in-window activity is an `agent_call`: both are listed, each
    attributed to its own project. The regression guard for the foreign-id `KeyError` in
    §5(a); `tests/test_ui_cost.py` has the `/cost` half,
    `test_the_default_window_survives_two_projects_with_os_calls`.

**`tests/test_ui_cost.py`** — the `client` fixture at test_ui_cost.py:26-29 and the
existing `transcript` (:32-48), `give_session` (:51-58), `add_recorded_turn` (:200-214) and
`add_os_calls` (:267-283) helpers. Timestamps must be chosen, so use
`FleetCostFixture.turn(...)`/`os_call(...)` through `fleet_fixture` for anything that needs
a turn in a PAST window; `add_recorded_turn` stamps `db.now()` and can only make the
current one.

14. `test_the_default_window_is_unchanged` — `GET /cost` with no parameters still says
    `usage week` and still lists the orders it lists today. The guard that this feature is
    invisible until something is picked.
15. `test_a_past_week_moves_both_halves` — one order with a turn last week, one with a
    turn this week. `?window=week&offset=-1` shows the first and NOT the second, in the
    per-order listing AND in the distribution panel; `?window=week&offset=0` is the
    mirror image. This is defect 3's acceptance test: the two sections move together.
16. `test_a_past_5h_window` — `?window=5h&offset=-3` renders, the label is a 5h span, and
    the anchoring sentence is on the page.
17. `test_a_custom_range` — `?since=…&until=…`; the datetime-local round-trips and the
    form is pre-filled with the active window.
18. `test_invalid_params_refuse_rather_than_falling_back` —
    `?window=5x`, `?offset=1`, `?offset=abc`, `?since=X&until=Y` with `since >= until`, and
    `?window=week&since=…`: each renders `error.html` with the §3 sentence, and NONE of
    them renders a week's figures.
19. `test_the_selector_survives_a_fleet_section_that_cannot_be_built` — the existing
    `monkeypatch` of `ops.fleet_cost` (test_ui_cost.py:482-491) plus `?window=week&offset=-1`:
    the listing and the window label are still on the page.
20. `test_the_window_is_in_the_url` — the prev/current links carry `project` and the
    resolved `offset`, and no `next` link is rendered at `offset=0`.

**`tests/test_cli_cost.py`** (or wherever `--fleet`'s CLI render is covered):
`--window`/`--offset` reach the payload, `--window` with `--since` refuses with the §3
sentence and a non-zero exit, and `--json` carries `local_label`.

### 9. Rejected alternatives

* **Let each surface resolve its own window from the raw query parameters.** Rejected by
  Neo's rider and by arithmetic: `resolve_window` is a function of `now`, so two calls a
  few milliseconds apart can land on either side of a boundary and the page would show two
  windows again — the exact defect being fixed.
* **Give `cost_report` `since`/`until` floats instead of the resolved dict.** Rejected: the
  page needs the LABEL and the `window`/`offset` for its links, so the floats would be
  re-resolved into a dict somewhere, and `source` would come back as `"flags"` for a past
  week — mislabelling the thing the reader picked.
* **Store the selection in a cookie or in `os.db`.** Rejected: it would not be shareable,
  the work order asks for query parameters, and a window in server state is a window two
  browser tabs disagree about.
* **Anchor the 5h grid on `claude_cli`'s parsed reset sentence.** Rejected: that parser
  reads a rate-limit MESSAGE (claude_cli.py:1198-1326), is present only after a limit was
  hit, and is about when the limit lifts — not about where a session started. Anchoring on
  it would make the grid exist only on throttled fleets.
* **Re-anchor the 5h grid per week when stepping back.** Rejected in §2: non-monotonic
  offsets, and overlapping or gapped windows at every week boundary.
* **Absolute `7 * 86400 * n` for weekly offsets.** Rejected in §4: slides the reset by an
  hour at every DST crossing, so a window stops being the usage week it claims to be.
* **A module constant for the 5h length.** Rejected by Neo's standing rider; it is a
  `CostConfig` field, per project.
* **Filter the transcript's calls before `classify_boundaries`.** Rejected in §5(c): it
  loses the boundary at the window's left edge, which is usually a cold start — the
  dearest event in the window.
* **Drop undated transcript messages silently, or count them as in-window.** Rejected: the
  first understates the bill without saying so and the second attributes spend to a window
  on no evidence. They are excluded and counted, which is the rule the rest of this report
  already follows for a running turn and for an unrecorded cost.
* **Window the bare `jarvis cost` listing too.** Rejected in §6: it would silently change
  the meaning of a long-standing command's output. The page is windowed because its two
  halves sit side by side; the terminal's two views are two commands.

### 10. Deliberately NOT in scope

* **No change to `bill.py` or to a single order's bill.** A bill must reconcile over the
  whole order; a windowed bill would not.
* **No turn id on `agent_calls`.** Jarvis-side spend is still attributed to a turn by
  timestamp interval, and the payload still discloses what could not be attributed. Named
  in the problem statement so narrowing the window is not mistaken for fixing it.
* **No alarm, inbox item or digest line.** This is a report someone opens; nothing raises
  a window.
* **No new percentile, metric or column.** The metrics are the parent spec's; only the
  population they are computed over becomes selectable.
* **No "compare two windows" view.** One window at a time. A diff is a second feature and
  it needs a decision about which figures are comparable across a mixed cost basis.

### 11. The DISPLAY zone (amendment)

Added after the selector shipped (commit 2fc541d, PR #972). The page could only ever be
read in one clock — `cfg.week_reset_zone` — so a reader in Madrid had to convert every
label by hand. `/cost` gains `?tz=<IANA name>`.

Decided before this amendment and not re-opened (Neo q1460):

* **DISPLAY ONLY.** Weekly and 5h BOUNDARIES stay anchored in `cfg.week_reset_zone`.
  Changing `tz` must move NO number: `since` and `until` in the resolved window are
  byte-identical whatever `tz` is. That is the load-bearing test
  (`test_a_picked_zone_moves_the_label_and_no_number`).
* **The page speaks ONE clock.** The custom range's two `datetime-local` inputs are
  rendered AND parsed in the picked zone, and the hint names the zone instead of saying
  UTC. The §6 justification for labelling that form UTC no longer holds, because the form
  is now parsed in the same zone it is rendered in.
* **An unknown or malformed zone is a REFUSAL**, matching the four window refusals of §3:
  `tz must be an IANA time zone name — {tz!r} is not a zone this report knows`. Never a
  silent fallback to the default.
* **URL-only**: no persisted setting, no cookie, for §9's reason.
* **The default comes from `cfg.week_reset_zone`.** No module-level default string.

The pieces:

* `fleetcost.resolve_zone(tz, cfg) -> str` — its OWN function, because the UI needs the
  zone BEFORE it can parse the form's naive datetimes and so cannot wait for
  `resolve_window` to return. Validates with `zoneinfo.ZoneInfo` and raises `OpsError` on
  `ZoneInfoNotFoundError`/`ValueError`. Empty is ABSENT, not bad: a blank form field is not
  a refusal.
* `resolve_window(..., tz=None)` calls it and adds an EIGHTH payload key, `zone` — the
  IANA name actually used — and renders `local_label` in it. `_local_label` takes the zone
  NAME rather than the config; the both-`%Z`-abbreviations rule of §4 is unchanged and now
  fires for a transition in the PICKED zone too. `report(tz=…)` and `window_of(tz=…)` pass
  it through; `ops.cost_zone(tz, project)` is the route's wrapper, beside
  `ops.cost_window`.
* `cost_page(..., tz="")` resolves the zone FIRST, converts the submitted naive
  `since`/`until` to epoch floats IN THAT ZONE (`_in_zone`), then resolves the window
  once as before. A string carrying an explicit offset is honoured as written. Every
  refusal stays inside the existing `try/except ops.OpsError`, so a bad `?tz=` renders
  `error.html`.
* `cost.html` gains a text `<input name="tz" list="tz-options">` with a datalist of common
  zones — a text input and NOT a `<select>`, so any IANA name is reachable. `tz` rides on
  every week/5h link and in both forms' hidden fields; the zone form carries a custom
  range with its OFFSET, so re-rendering it in a new zone moves no boundary.
* `jarvis cost --fleet --tz <IANA>`, so the CLI label and the page label agree.

**`fleetcost._as_ts` and `compaction_payoff.parse_when` are NOT touched: a naive `--since`
on the CLI stays UTC.** Redefining an existing flag's clock is not in scope, and the page
is the surface that now has a zone control. The asymmetry is deliberate and is stated in
`--tz`'s help.

`PAYLOAD_VERSION` stays 1 for §7's reason: `window.zone` is additive, and `local_label` is
a label whose meaning (the span in the zone being displayed) is unchanged at its default.
