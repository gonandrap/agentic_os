# A closed issue the OS cannot move converges

GitHub issue 961. Work order wo-338df90b. Fix ruled by Neo question 1410; the alternative
in "Rejected alternatives" is recorded as refused, not open. History of the recurrence:
kn-cd6be773, kn-ffb94e3a (`jarvis learn show <id>`).

## The problem

**The issue sweep never converges when the want is not CLOSED and the issue already is.**
It is a comparison with no fixed point, so it replays for ever — and the replay is read as
a landing.

The loop, each half correct on its own:

1. `Daemon.sync_issues` (src/jarvis/daemon.py:7689-7754) is a comparison, by its own
   docstring: `want = issues.desired_state(store, wo)` against the `issue_state` column,
   `continue` while the two agree (daemon.py:7716-7718).
2. `issues.apply` (src/jarvis/issues.py:741) re-reads the issue and returns CLOSED
   whenever the tracker issue is already closed — `if issue.closed: return CLOSED`,
   issues.py:777-778 — whatever the want was. That is deliberate: "IT NEVER FIGHTS A
   HUMAN" (issues.py:748-753).
3. `issues.record_applied` (issues.py:1093-1105) stores that returned CLOSED in
   `issue_state` (issues.py:1102) and writes an `issue_closed` timeline event
   (issues.py:1103-1104).

So for an order whose desired state is IN_PROGRESS or RELEASED over an issue that is
CLOSED, the stored value is CLOSED and `want != stored` on every tick, for ever. Each tick
costs one `gh issue view`, one `is now closed` log line and one duplicate `issue_closed`
event, per order.

Measured, production 0.10.47: issues 792, 836, 837, 843, 903 all in this loop; **315
`is now closed` lines for 837 alone** in ~10h from 2026-10-06 20:00.

### The expensive half: the replay is the release trigger

`sync_issues` reads `applied == issues.CLOSED` as THE LANDING SIGNAL and calls
`ensure_release` (daemon.py:7749-7754). That is a no-op while a release order is open —
`ensure_release` batches onto it (daemon.py:7859-7921) — so each replay only re-joins the
live batch. **The moment that batch settles, the next tick files a fresh release order.**

Live: wo-5f35e667 (2026-10-06 20:19) then wo-10fbcea5 (2026-10-07 05:39). This is the
already-live-release-order recurrence of kn-cd6be773 and kn-ffb94e3a, arriving by a route
neither of them closed.

The assumption it breaks is written down twice and relied on in both places:

* `_already_live` (daemon.py:7929-7959): *"The call is one-shot — it fires on the
  issue-state transition and never comes back — so an unreadable repository or production
  checkout falling through is mandatory"*.
* `hold_red_release` (daemon.py:7978-7987): *"`ensure_release` fires only on the
  issue-state TRANSITION, so a release skipped because `main` was red would never be filed
  again"*.

Both rest on a transition that the code never actually enforces. It was true only because
nothing had produced a stuck non-CLOSED want before.

### How a live order reaches a non-CLOSED want over a closed issue

`issues.desired_state` (issues.py:676-705) returns RELEASED for a `completed` order when
`store.work_unlanded_open(wo_id)` is true (issues.py:703-705,
src/jarvis/project_store.py:3803): a landing refusal re-parked an order whose issue the OS
had already closed. Nothing in that path is wrong; it is simply a want the tracker cannot
be moved to.

### Root cause, named

**`sync_issues` compares a DESIRED state against an APPLIED state, and `apply` is allowed
to return a state that is not the desired one.** A comparison between two different
quantities has no fixed point. The fix adds the missing terminal case rather than making
`apply` lie or making `desired_state` follow the tracker.

## The fix

Three parts. (1) stops the replay, (2) makes the one-shot assumption true, (3) states what
the stop deliberately gives up.

### 1. `issues.needs_sync` — the convergence latch, out of `desired_state`

`issues.desired_state` STAYS PURE: derived from the work order only, never from
`issue_state`. Neo question 1410's ruling.

New predicate in `src/jarvis/issues.py`, beside `desired_state`:

```python
def needs_sync(store: Any, wo: dict[str, Any]) -> bool:
```

True when there is something the sweep may still do to this issue. Two rules:

1. A stored `issue_state` of `CLOSED` is **TERMINAL** — return False. A closed issue is
   where a person (or a previous converged pass) put it, and the OS has nothing left it
   may do there: `apply` will return CLOSED again, the column already says CLOSED, and the
   comment was posted on the sweep that discovered the close (issues.py:769-772).
2. Otherwise the existing comparison: `desired_state(store, wo) != (wo.get("issue_state")
   or "")`.

`store` is a parameter because `desired_state` needs it; the daemon's local `want` goes
away with the comparison it fed.

Call site: `sync_issues` replaces the bare comparison at daemon.py:7716-7718 with

```python
if not issues.needs_sync(store, wo):
    continue
```

The latch lives in `issues.py` and not in the daemon because `record_applied` has a second
caller — the filing path, `promote_confirmed` (issues.py:1358) — and the rule about what
the sweep may still do belongs in the module that owns the policy, where both can read it.

### 2. `ensure_release` fires only on a REAL transition

In `sync_issues`, capture the column BEFORE the pass and add it to the release condition
(daemon.py:7749-7753):

```python
was = (wo.get("issue_state") or "")
...
if (applied == issues.CLOSED and was != issues.CLOSED
        and ops_mod.routes_on_pull_request(store, wo)
        and (issues.dispatches(...) or issues.was_expedited(wo))):
```

**This is what makes the one-shot assumption TRUE rather than assumed.** `_already_live`
and `hold_red_release` both document a fire-once-on-transition contract; until now nothing
checked that the state had in fact changed, and part 1 alone would not fix it — an order
that reaches CLOSED legitimately still passes `needs_sync`, and a second route into this
shape would re-fire the release again.

The gate is in `sync_issues` and not inside `ensure_release`: `ensure_release`'s job is
"get a release carrying this fix on its way", and "is this landing NEW" is the sweep's
knowledge — only the sweep holds the pre-pass column value. `sync_issues` is also its only
production caller (the other two are tests and the direct filing helper), so the gate
covers everything without constraining a direct call.

### 3. What the latch deliberately gives up

With the order's own `issue_state` latched at CLOSED, **a human REOPENING that issue is
not re-labelled by this sweep.**

That is the EXISTING rule, not a new loss: `apply`'s docstring already states it
(issues.py:750-753 — "Reopening is therefore a person's prerogative in both directions"),
and `test_an_issue_a_human_reopened_is_left_reopened` (tests/test_issue_lifecycle.py:984)
pins it. The latch reaches the same outcome one step earlier, without the `gh` call.

## Tests

In `tests/test_issue_lifecycle.py`, written in parallel. What they must PROVE:

1. **Convergence.** Consecutive sweeps over a human-closed issue under a LIVE work order
   (non-CLOSED want): the first tick behaves as today; the **second tick makes zero `gh`
   calls, logs nothing, writes no second `issue_closed` event, and calls `ensure_release`
   zero times.** All four, not just the event count — the log line and the `gh` call are
   the measured cost (315 lines for issue 837).
2. **No recurrence.** Settle an open release order, then tick again: **NO further release
   order is filed** for a fix whose issue closed on an earlier pass.

Both must FAIL on 0.10.47. A test that passes before the change is not testing this
defect: the first proves the replay, the second proves the batch-settles-then-refiles
recurrence, and that recurrence needs the release order settled between the two ticks.

## Rejected alternatives

* **Make `desired_state` return CLOSED once `issue_state` is CLOSED.** The obvious fix —
  the comparison converges with no new predicate. Refused, Neo question 1410: `apply`
  calls `desired_state` itself (issues.py:762) and the filing path calls `apply` directly
  through `record_applied` (issues.py:1358), so an issue a human had REOPENED would be
  seen as wanting CLOSED and **re-closed** — fighting the human in the other direction, to
  fix fighting them in this one. The latch belongs where the sweep decides whether to act,
  not in the policy both paths derive from.
* **Have `apply` raise instead of returning CLOSED on a non-CLOSED want.** The caller
  records nothing and retries, which is the loop with an exception in it: same `gh` call
  every tick, plus a warning path (`_warn_issue_sync_broken`) telling the user the tracker
  is broken when nothing is.
* **Part 2 alone — gate the release on the transition and leave the replay.** Stops the
  money and leaves the 315 log lines, the duplicate `issue_closed` events and one
  subprocess per order per tick. The timeline is the record the user reads.

## Out of scope

* **The #934 liveness guard failing open because `os_owner` resolves to shared_schedule.**
  Real, and it is why wo-10fbcea5 was filed rather than suppressed as already-live. Filed
  as issue #956 with its own work order. No fix proposed here.
* The five issues already in the loop (792, 836, 837, 843, 903). The latch converges them
  on the next reconcile tick; no migration and no backfill of the duplicate
  `issue_closed` events.
* Any change to `apply`'s never-fight-a-human rule, to `desired_state`'s three answers, or
  to `ensure_release`'s batching.
