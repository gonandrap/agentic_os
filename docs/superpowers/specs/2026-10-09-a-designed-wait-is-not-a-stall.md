# A designed wait is not a stall

Work order `wo-eeff70d3`, GitHub issue 1009.

## 1. The problem

`ops.stuck_scan` (`src/jarvis/ops.py:9048`) judges every `OPEN_STATUSES` order, excluding
only `UNGOVERNED_ORIGINS` and the investigation kinds. A `pending` order whose `depends_on`
edges are all still LIVE is therefore judged on the status clock against the 30-minute
`pending` threshold and declared stuck — while it is waiting exactly as `jarvis wo create
--depends-on` designed it to.

`invariants.true_blockers` is deliberately silent for live ordinary blocking (a merely-live
edge is not an attention item; `invariants.py:1118` raises only on
`dead_dependencies`). So the sweep's own row renders the blocker as
`"nothing the record can name"` (`ops.py:9095`), and `stuck.WHY` hands an investigator a
subject with no named blocker — a model session bought to be told the OS is working.

Evidence, 2026-10-09: ONE sweep opened 11 investigations across one feature order's
dependency graph, two of them on the subject's own live blockers, against
`fleet_health.max_per_day=48`. Nearly a quarter of the fleet's daily investigation
allowance spent on correct waiting.

Root cause: `stuck.assess`'s `excluded_cause` family (`src/jarvis/stuck.py:76-81`) names
three self-clearing holds — the account usage window, a live `jarvis pause`, the
post-reopen ramp — and a live blocking dependency is a fourth with the same property
(it clears by itself when the dependency completes; the daemon completes the parent
within ~2min of a merge). It was never added, so the sweep has no way to say so.

## 2. The fix

### 2.1 The named cause — `src/jarvis/stuck.py`

A module-level constant next to `ACTIVITY_STATUSES`:

```python
#: The fourth self-clearing hold of §2's `excluded_cause` family: this order's
#: `depends_on` edges are all LIVE, so it is waiting as `--depends-on` designed and the
#: wait ends without anyone acting. A DEAD edge is not this cause — see §2.3.
LIVE_DEPENDENCY = "live_dependency"
```

Name: `live_dependency` — "live" is the whole discrimination being made (live edges
exclude, dead edges do not), and it reads correctly in the sentence `assess` already
builds: `held by live_dependency, which clears by itself — nothing is judged while it
does`.

`assess`'s docstring §2 paragraph gains this as the fourth member. `assess`'s signature,
branching and `Verdict` are otherwise UNCHANGED: the predicate stays pure and reads no
store.

### 2.2 The read — `ops.stuck_scan`

For a `pending` order ONLY, before building the `excluded_cause` argument, read the edges:
`pstore.unfinished_dependencies(wo["id"])` (`project_store.py:2324`). Pass
`stuck.LIVE_DEPENDENCY` when there is at least one edge and NONE of them is dead by the
predicate in §2.3.

Precedence: the existing `PAUSE_USAGE_LIMIT` cause keeps it. The window is the account's
and outranks any per-order hold; the expression is `PAUSE_USAGE_LIMIT if window or
any(h.open for h in usage) else (stuck.LIVE_DEPENDENCY if … else "")`.

Why here and not in `assess`: `assess` is pure over scalars by ruling
(`stuck.py:1-14`, `health.due`'s docstring) — "reading state inside it would make that
decision untestable without a store". The store read belongs at the one call site that
already holds a `ProjectStore`. Why `pending` only: no other open status has blocking
edges holding dispatch, and reading them elsewhere would spend a query per order per tick
for an answer that can never be yes.

### 2.3 One definition of dead — `src/jarvis/invariants.py`

A dead edge (`project_store.DEPENDENCY_DEAD_STATUSES`, or status `"missing"`) is NOT
excluded: it cannot clear itself, and it still raises `DEAD_DEPENDENCY_BLOCKER`
(`invariants.py:681`, `1118`) — such an order SHOULD be judged, and the record can name
its blocker.

Factor the per-dependency test out of `dead_dependencies` (`invariants.py:1601`) into a
module-level predicate in the same module:

```python
def dependency_is_dead(dep: Mapping[str, Any]) -> bool:
    """One row of `ProjectStore.unfinished_dependencies`: can this edge NEVER clear?"""
    return dep["status"] in DEPENDENCY_DEAD_STATUSES or dep["status"] == "missing"
```

`dead_dependencies` becomes `[dep for dep in store.unfinished_dependencies(wo["id"])
if dependency_is_dead(dep)]` — behaviour identical, callers
(`ops.py:2327`, `2722`, `7822`, `11467`, `remedies.py:234`) untouched. `stuck_scan` calls
the same predicate. Two copies of "dead" could drift apart and leave an order both
excluded from the sweep and flagged dead, which is the one incoherent outcome.

## 3. Out of scope

- `true_blockers`' deliberate silence on live ordinary blocking. Unchanged — an excluded
  order is not reported at all, so it needs no blocker string.
- The `DEAD_DEPENDENCY_BLOCKER` path, `jarvis wo unblock`, and attention derivation.
- Every `fleet_health.thresholds` default, including `pending`'s 30 minutes. The defect is
  not that the number is too small.
- No new catalog setting. This is a KIND of hold, like the usage window; `excluded_cause`
  has never been tunable and a number here would invite switching correctness off.

## 4. Tests the implementer must write

Extend the existing files; do not create new ones.

`tests/test_health_sweep.py` — holds the sweep's exclusion tests; follow
`test_usage_limit_hold_is_not_stuck:1297` and `test_held_seconds_do_not_count:1320`, and
reuse the `stuck_os` fixture and the `park_order` / `usage_hold` helpers (`:1260-1294`):

1. `pending` past threshold with one live edge: `verdict.stuck is False` and
   `verdict.excluded == stuck.LIVE_DEPENDENCY`.
2. `pending` past threshold with two edges, one of them `cancelled` (and a second case,
   a DELETED dependency rendering as `"missing"`): still judged, still `stuck is True`,
   `excluded == ""`.
3. `pending` past threshold with NO edges: stuck, exactly as today.
4. A non-`pending` open status (`waiting_pr_merge`) on an order that HAS edges: judged
   unchanged — proves the read is gated on status, not merely on edge presence.
5. Usage-window precedence: `pending`, live edges AND an open usage hold —
   `excluded == PAUSE_USAGE_LIMIT`, not `LIVE_DEPENDENCY`.

`tests/test_investigation_orders.py` — the subject list and the cap; follow
`test_stuck_order_gets_exactly_one_investigation:1475`,
`test_an_investigation_is_never_a_subject:1609` and `test_daily_cap_holds_fleet_wide:1522`:

6. After `daemon.stuck_tick(None)` over a `pending` order with live edges past threshold:
   `ops.list_investigation_orders(..., include_settled=True) == []`, and the day's filed
   count is unmoved — an excluded order spends no part of `max_per_day`.

`tests/test_wo_dependencies.py` (`:194`, `:209`, `:224`) must pass untouched: that is the
assertion that §2.3's refactor changed nothing about `dead_dependencies`.
