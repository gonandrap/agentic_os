# A release blocked by a red `main` retries itself

Work order wo-7a664ec4, GitHub issue #838. Design ruled by Neo, question 1010.

## The problem

Live case: **wo-fc61d0cd**, the release for #826 batching #823. Gate 310 approved,
`scripts/shipit.sh --stage` ran, and `main` turned red under it (5a4e14d, #829) while it
waited for CI. The worker refused to ship an older green commit — correct — and delivered
"Release BLOCKED on a red main". Two defects followed.

**(a) The blocked release asked the user instead of waiting.** The order settled
`needs_review` with an assumption. #807 defers a release order's DISPATCH while the base
is red (`Daemon.hold_red_release`, `src/jarvis/daemon.py:6997`), but that step only
visits `pending` rows:

```python
for candidate in store.list_work_orders(statuses=("pending",), include_hidden=True):
```

A release that goes red MID-RUN is `running`, has already spent its gate approval, and
has no path back to `pending`. Nothing in the OS re-runs it when `main` recovers, so a
release that was seconds from shipping ends as a decision the user owes — and the fix the
fleet expedited sits on `main` unreleased until a human types something.

**(b) The panel judged a submission with nothing in it.** A blocked release authors no
files and stages no tag, so `evidence.nothing_to_judge`
(`src/jarvis/evidence.py:391`) hits row 2 of its table and returns `"escalate"`; the
work-order validation loop (`src/jarvis/daemon.py:2021-2027`) escalated with

> this submission changes no files and records no other durable effect, so there is
> nothing to review. Nobody has judged the work.

which put a `panel_gave_up` hold (`autoreview.HELD_PANEL_GAVE_UP`,
`src/jarvis/autoreview.py:131`) on the assumption, so even Neo could not clear it. A
SUCCESSFUL staged release escapes this: `ops._release_effects`
(`src/jarvis/ops.py:11829`) collects an attested `release_staged` effect and the packet
is voided instead. Only the BLOCKED release falls through to `escalate`.

Root cause of both: **the OS has no concept of a release order that delivered no
release.** It is treated as an ordinary delivery — judged like one, parked like one —
when the only true statement about it is "the base branch was not buildable, ask again
later".

## The fix

Five pieces. Sections 1-3 are Neo's ruling; 4 and 5 are consequences found in the code.

### 1. `finish` re-parks a release that delivered nothing, off the STORED reading

New function `ops.defer_red_release(store, project_name, wo) -> str | None`, next to
`ops.park_unlanded` (`src/jarvis/ops.py:4110`) — its sibling: "hold a work order that
would otherwise land, and SAY SO". Called from `ops.finish`
(`src/jarvis/ops.py:5024`) AFTER the `finished` event is written (line 5112, so
`refusal_answered` still dates correctly) and AFTER the `abandon` branch, BEFORE the
validation/landing block at line 5135. It returns a status when it took the order, and
`None` to fall through to today's behaviour.

Predicate for "this is a release order": `release_for_issues` in `metadata`. Move the
literal from `Daemon.RELEASE_BATCH_KEY` (`src/jarvis/daemon.py:6904`) to
`release.BATCH_KEY` and keep `Daemon.RELEASE_BATCH_KEY = release.BATCH_KEY` as an alias —
`release.py` is already imported by both `ops` and `daemon`, and `ops` importing `daemon`
at module level would be a cycle.

Predicate for "delivered NO release": `ops._release_effects(store, wo_id) == []`. That
function already unions the staged-release marker and the `release_verified` /
`release_restart` timeline events, so an empty list means no tag was claimed from either
source. **Any release effect, verified or not, counts as delivered** — a claim that is
unverified because the restart has not happened yet must not be re-dispatched into a
second `shipit.sh` run.

The reading is `CentralStore.base_health(project_name)`
(`src/jarvis/central_store.py:1806`), written per tick by `Daemon.poll_default_branch`
(`src/jarvis/daemon.py:4703-4714`). **Never a live `gh` call in the worker's finish
turn** — rejected because it would make a delivery's outcome depend on GitHub being
reachable at that second, and would duplicate the one rate-limited reader the OS has.

Decision table, applied to the stored fact:

| stored reading | what `defer_red_release` does |
|---|---|
| `red: true`, fresh | re-park (below) |
| absent (`{}`), unparseable (`base_health` already answers `{}`), or `checked_at` missing / older than `central_store.BASE_HEALTH_FRESH_SECONDS` (900) | re-park, identically |
| `red: false` AND `checked_at` within 900s | return `None` — today's `needs_review` |

Neo's explicit condition: **only a FRESH GREEN reading parks on the user.** An unusable
fact is not evidence that `main` is fine; the daemon's own next tick will write a real
one, and re-parking costs one dispatch that a green base makes succeed. Nothing fabricates
a verdict from an unreadable reading — the freshness test is
`Daemon._base_red`'s verbatim (`src/jarvis/daemon.py:4780-4784`).

The re-park itself:

```python
store.set_status(wo_id, "pending", trigger="release_red_base")
store.hold_dispatch(wo_id, until=db.now() + Daemon.RED_HOLD_SECONDS)
# one event per BROKEN COMMIT, deduped on head_sha exactly as `_say_base_is_red` does
store.add_event(wo_id, RED_DEFER_EVENT, {...})
```

* `hold_dispatch` (`src/jarvis/project_store.py:2181`) writes `retry_after` ONLY and must
  not touch `dispatch_attempts`: that ladder's author is `release_dispatch_claim`, and a
  deferral for a reason outside the order must not spend a launch.
* **NO attention flag and no notification.** A red base for a few minutes is ordinary —
  `_say_base_is_red`'s rule (`src/jarvis/daemon.py:7083`), and `pending` raises no blocker
  in `invariants.true_blockers` unless a dependency is dead.
* Nothing calls `clear_attention`: there is no flag to clear on a `running` order, and
  clearing would overwrite an `acknowledged_blockers` decision.
* `set_status` already suppresses a move to the status the order is in, so a repeat is
  silent by construction.

From there **`Daemon.hold_red_release` owns it unchanged**: the order is now `pending`
with `release_for_issues` in its metadata, which is exactly the population that step
selects. It extends the hold every `RED_HOLD_SECONDS` while red and clears `retry_after`
on the first green tick, and `claim_next_pending`'s guard 4
(`src/jarvis/project_store.py:2229`) is what actually holds the dispatch.

Rejected: **teaching `hold_red_release` to also visit `needs_review`.** It would have to
un-park an order the user has already been asked about, and it is a DISPATCH filter by
design (Neo question 794). The mid-run case is a decision at DELIVERY time and belongs
where deliveries are decided.

Rejected: **a new status** (`waiting_base_green`). `pending` + `retry_after` already means
exactly this, and `project_store.py:281-285` is the standing rule: a condition that can be
derived does not earn a status.

### 2. A release order opens no validation round, at either submission site

One predicate, two call sites — matching `finish`'s own docstring, which states that
`os.validation.enabled` is read at the submission sites ONLY and that adding a check in
`daemon.validation_tick` for symmetry is a bug:

```python
def validation_applies(cfg, wo) -> bool:          # src/jarvis/ops.py, beside _validates_on_review
    return cfg is not None and cfg.enabled and not release.is_release_order(wo)
```

* `ops.finish` (`src/jarvis/ops.py:5136`) — replaces `if cfg is not None and cfg.enabled`.
* `ops._validates_on_review` (`src/jarvis/ops.py:6385`) — the condition `review_work_order`
  reaches through `_land_after_acceptance`. It is a catch-up path for orders parked before
  two-gates shipped, and a release order among them must not be sent to a panel now either.

**And `land_when_cleared` is TOLD.** Both sites pass `panel_cleared=True` when validation
was skipped for a release order, so the join does not re-read a round that was never
opened (`src/jarvis/ops.py:4195`, and `panel_cleared`'s docstring: it exists precisely for
a caller that has settled the panel's half itself). Without it a stale `failed` or `void`
row from a forced round would read as "in flight" and park the release in `validating` for
ever. The assertion `not (panel_cleared and panel_open)` holds: the bounce path is
unreachable here, because a bounce needs a previously judged round.

Why no panel at all, rather than a release-specific seat: **the post-condition already
exists and is a machine check.** `Daemon.settle_shipped_releases` /
`_settle_shipped_release` (`src/jarvis/daemon.py:7109`, `:7156`) requires a `jarvis-*`
tag containing every payload commit of the batch AND production running that tag; a seat
reading a release worker's prose adds nothing to that and is the theatre the empty-packet
guard exists to prevent.

Rejected: **widening `evidence.nothing_to_judge` to void a blocked release.** That guard
has already been widened twice for the same lesson (the registry comment at
`src/jarvis/ops.py:11963`), and `void` means "the OS verified this itself" — asserting it
over a release that verifiably did NOT happen is the one direction that must never happen.
`nothing_to_judge` is left exactly as it is; it simply stops being reached.

### 3. The long threshold, and what the attention line says when it fires

```python
#: How long `main` may stay red before a release order stops waiting and asks the user.
#: Measured from the FIRST red hold on that order, so the pending-dispatch hold (#807)
#: and the mid-run re-park above share one clock.
RED_PARK_AFTER_SECONDS = 6 * 3600
```

Beside `RED_HOLD_SECONDS` in `daemon.py:6991`, read lazily by `ops.defer_red_release`
(the `from .daemon import ...` inside-a-function idiom `escalate_validation_round` already
uses). The clock is the earliest `Daemon.RED_HOLD_EVENT` (`release_held_red_base`) OR
`RED_DEFER_EVENT` row on the order — the two paths write different kinds and both count.

**Recommend 6 hours.** A red `main` on this repo is repaired by a work order round trip
(file the bug, dispatch, PR, merge), which is hours and not minutes; one hour would ask
the user about a break the fleet was already fixing, which is exactly the noise this
feature removes. 24h would let a break nobody is repairing swallow a whole day while an
expedited fix the user asked for in production never ships. 6h costs at most 72 re-park
cycles at `RED_HOLD_SECONDS` and no `gh` calls beyond the one the daemon makes anyway
(an in-force hold IS the rate limit).

Past the threshold `defer_red_release` parks instead: `needs_review`, one
`release_park_red_base` event, one `flag_attention`. Episode discipline is
`park_unlanded`'s (`src/jarvis/ops.py:4110-4148`) and kn-7b122cd9's: the event and the
flag are written only when no park episode is open (a park with no later `finished` /
`abandoned` / `release_completed`), so nothing renotifies.

**The attention text names the RED RUN, not the release**, and must therefore be DERIVED —
`INV-ATTENTION-REASON` rewrites any reason `invariants.true_blockers` does not produce.
So: a new constant `invariants.RELEASE_BASE_RED_BLOCKER` and a branch in `true_blockers`
(`src/jarvis/invariants.py:664`), placed beside the `pending`/`DEAD_DEPENDENCY_BLOCKER`
branch at `:768` and gated on `wo["status"] == "needs_review"` plus an open
`release_park_red_base` episode, so no other work order pays a query. The payload carries
`workflow`, `head_sha`, `run_url` — all already in the stored `base_health` fact
(`src/jarvis/daemon.py:4703-4713`) — and renders as:

> `main` has been red for 6h — `ci` failed at 5a4e14d0 (<run url>). The release is waiting
> on that build, not on anything about the release.

### 4. Coalescing with #784/#807: no gap. Verified, not assumed

`Daemon.settle_shipped_releases` iterates `OPEN_STATUSES`
(`src/jarvis/daemon.py:7139`), and `pending` is the FIRST entry of that tuple
(`src/jarvis/project_store.py:65`). `_settle_shipped_release` (`:7156`) has no status
guard at all — its refusals are the marker naming this order, an unresolvable payload, no
tag, and a tag not yet live. `release.settle` (`src/jarvis/release.py:342-384`) special-
cases only `completed`, `waiting_pr_merge` and pending assumptions; a `pending` order
falls to `ops.close_out`, which is right.

So a release order deferred into the new `pending` state IS settled when a newer release
ships its batch. **Nothing to fix.** What is missing is the pin: today that works by
accident of one tuple's contents, and an edit to `OPEN_STATUSES` or a status guard added
to `_settle_shipped_release` would break it silently. Test 8 below is the regression.

### 5. The re-dispatch has to reuse the conversation

The one thing the ruling implies that the code cannot do yet. A re-parked order still
carries `session_id` and `worktree`, and `worker_session.start`
(`src/jarvis/worker_session.py:328-344`) unconditionally passes `resume=False` with that
same id and `--worktree <wo-id>`:

* `--session-id` on a session that exists is refused by the CLI (`busy`'s docstring,
  `src/jarvis/worker_session.py:236-244`).
* `--worktree` is the flag that CREATES the worktree, so passing it over an existing
  directory asks for one that is already there (`retry`'s docstring,
  `src/jarvis/worker_session.py:1244-1250`).

Fix: `start` re-decides both from the filesystem exactly as `worker_session.retry` does at
`:1247-1263` — `resume=_conversation_started(project, wo)`, `worktree=None` when
`worktree_path(project, wo)` exists, `cwd` that worktree. For every ordinary dispatch
(no session, no worktree) this is byte-identical to today's argv.

Rejected: **nulling `session_id` and `worktree` on the re-park.** It throws away the
conversation that holds the approved gate 310 and the reasoning about the red base, pays a
full cold-start cache write, and orphans a worktree nothing deletes.

The `RED_HOLD_SECONDS` hold also covers the seconds between `finish` returning and the
worker's turn ending, so no tick can dispatch over a live turn.

## Tests

One per behaviour. New file `tests/test_release_red_defer.py` unless named otherwise.

1. `test_red_reading_reparks_release_to_pending` — stored `base_health` red and fresh,
   release order with a batch and no release effect finishes: status `pending`,
   `retry_after` ≈ `now + RED_HOLD_SECONDS`, `dispatch_attempts` unchanged (0),
   `attention_reason` NULL, one `RED_DEFER_EVENT` carrying the head sha.
2. `test_unusable_reading_reparks_like_red` — parametrized over absent (`{}`),
   unparseable JSON, `checked_at` missing, and `checked_at` older than
   `BASE_HEALTH_FRESH_SECONDS`: every case lands identically to test 1 and NEVER in
   `needs_review`.
3. `test_fresh_green_reading_parks_on_the_user` — `red: false`, `checked_at` now: status
   `needs_review`, no `RED_DEFER_EVENT`, no `retry_after`. The only route to the user
   inside the threshold.
4. `test_green_tick_clears_the_hold_and_dispatch_reruns_it` — after test 1's state, run
   `Daemon.hold_red_release` with a green `ci.base_runs` fake, then `dispatch_pending`:
   `retry_after` cleared and the order claimed to `dispatching`, with the second turn
   resuming the same `session_id` (asserts §5).
5. `test_past_the_long_threshold_it_parks_naming_the_red_run` — first `RED_HOLD_EVENT`
   backdated past `RED_PARK_AFTER_SECONDS`, base still red: status `needs_review`, ONE
   `release_park_red_base` event, and `invariants.true_blockers` returns
   `RELEASE_BASE_RED_BLOCKER` containing the workflow, the short head sha and the run url.
   Second call with nothing changed writes no second event and re-flags nothing.
6. `test_release_order_opens_no_round_on_finish` (`tests/test_validation_release_skip.py`)
   — validation enabled, release order delivers a staged release (attested effect
   present): `latest_validation_round` is None, status is not `validating`, and no
   `validation_escalated` event exists. This is defect (b).
7. `test_release_order_opens_no_round_on_review`
   (`tests/test_validation_release_skip.py`) — same order parked in `needs_review` with a
   pending assumption; `ops.review_work_order(accept=True)` opens no round and lands it.
8. `test_overtaken_settles_a_release_deferred_in_pending`
   (`tests/test_release_overtaken.py`) — the §4 regression: a release order in `pending`
   with `retry_after` in the future, whose batch a newer `jarvis-*` tag carries and which
   production runs, is `completed` by `settle_shipped_releases`.

Two supporting assertions, folded into 1 and 5 rather than given their own functions:
`hold_dispatch` leaving `dispatch_attempts` alone, and the head-sha dedupe writing one
event per broken commit and two when `main` breaks again on a different commit.
