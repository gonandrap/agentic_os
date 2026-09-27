# An expedited order that lands ships a release

wo-88bb9727. Supersedes the release half of
`2026-09-25-an-expedited-filing-dispatches-now.md`; leaves its rating half intact.

## The problem

**An expedited fix that lands ships nothing, so a human has to watch for the merge and
cut the release by hand — and when the watching session moves on, nobody does.**

Evidence, one session, 2026-09-26/27: #797 (wo-956c29bc, PR #800) and #784 (wo-521c9225,
PR #802) both merged to `main` and both sat unreleased ~7h. Nothing was broken in the
code; the operator had stopped watching.

Root cause, one clause. `Daemon.sync_issues`, at its tail, gates the release on the
RATING:

```python
if (applied == issues.CLOSED
        and ops_mod.routes_on_pull_request(store, wo)
        and issues.dispatches(wo.get("issue_priority") or "")):
    self.ensure_release(project, store, wo)
```

`issues.dispatches` is true only for `critical`/`blocker`. And `issues.route_filing`
deliberately writes `"priority": "" if dispatches(priority) else priority` into
`promote_confirmed` for an expedited filing — so an expedited order is the one case that
carries EITHER an honest low/medium rating OR no rating at all. Both fail that clause.
Expediting is the user saying "this goes to production now"; the column that decides the
release cannot see that they said it, because it is not recorded anywhere.

Two facts follow and both are the same defect:

1. **No durable record of expediting.** `route_filing` returns `out["expedited"] = True`
   to the caller and drops it. The work order row keeps `issue_url`, `issue_priority`
   and a brief mentioning the word; nothing queryable.
2. **No consumer.** Even with a flag, `sync_issues` reads only `issue_priority`.

Not the root cause, and deliberately unchanged: a CLAIMED `critical`/`blocker` that Neo
has not confirmed still earns no release on its own. That rule is right and its tests
stay.

Second, smaller problem, which only exists once the first is fixed: an expedited `low`
fix lands far more often than a confirmed blocker does, so the OS will now cut releases
unattended and routinely. Today nothing stops one going out on a red `main` except a
sentence at the bottom of `Daemon.RELEASE_BRIEF` telling the worker to check. That is a
worker's judgement standing in for a post-condition.

## The fix

### 1. The flag: `metadata["expedited"]`

`issues.EXPEDITED_KEY = "expedited"`, a new module constant in `src/jarvis/issues.py`
beside `EXPEDITED_WHY`.

`issues.promote_confirmed` grows `metadata: dict[str, Any] | None = None` and passes it
straight to `ops.create_work_order`, which already takes `metadata` and already forwards
it to `ProjectStore.create_work_order` AT CREATION (its comment says why: the daemon can
claim and dispatch before a follow-up write lands). No new column.

`issues.route_filing`, in its `if expedite:` branch only, passes
`metadata={EXPEDITED_KEY: True}`.

Why metadata and not a column: it is one boolean read by exactly one daemon step and by
`jarvis wo show`; nothing joins or sorts on it. Same argument `RELEASE_BATCH_KEY`
already makes in its own comment.

Why on the work order and not the issue: the issue's labels are the RATING, which is
Neo's to move (`settle_work_order_priority`). Expediting is the user's scheduling act
and must survive a downgrade. Putting it on the order makes that structural rather than
a rule someone has to remember.

Independence from priority is then automatic: nothing in `settle_triage` /
`settle_work_order_priority` touches `metadata`.

`issues.was_expedited(wo: dict) -> bool` — reads `db.from_json(wo.get("metadata"), {})`
and coerces. One helper so the daemon does not hand-roll the parse, and so a row written
before this shipped reads `False`.

### 2. The trigger: one clause in `Daemon.sync_issues`

```python
and (issues.dispatches(wo.get("issue_priority") or "")
     or issues.was_expedited(wo))
```

Nothing else in that tail moves. `applied == issues.CLOSED` stays the landing signal —
`issues.desired_state` reaches CLOSED only for a merged pull request — and
`ops_mod.routes_on_pull_request` still keeps orders with nothing to land out.

Why here and not on `complete_merged` or on a `completed` status transition: this is the
only place in the OS that knows the code is ON `main` rather than on a branch, and it is
the place the confirmed-blocker route already fires from. A second trigger point would
be a second definition of "landed" (issue #232's distinction) and the two would drift.

`self.ensure_release(project, store, wo)` is called UNCHANGED.

### 3. Batching and settlement: inherited, zero new code

`Daemon.ensure_release` already scans open orders for `metadata[RELEASE_BATCH_KEY]` and
either appends `issue_url` + a description line + a `release_batched` event, or files a
fresh order. It is idempotent on the url. An expedited landing therefore coalesces into
an open release order exactly as a blocker landing does, whatever cut that order.

`Daemon.settle_shipped_releases` / `_settle_shipped_release` / `_release_payload` /
`_merge_commit_of` (issue #784) key off `RELEASE_BATCH_KEY` and `release_batched` and
never look at what caused the order. An expedited-origin release order is
indistinguishable from a blocker-origin one to all of them, so overtaken-detection,
the tag-and-deployed rule and the `OVERTAKEN_EVENT` tag dedupe all apply with no edit.
This section exists to say so: nothing in #784 changes, and any change there would be
the bug.

### 4. `Daemon.RELEASE_BRIEF`: one true sentence per route

Its opening sentence is now false for an expedited `low`:

> These fixes have LANDED on `main` and the bugs they close were confirmed `critical` or
> `blocker` by Neo, so the OS owes the fleet a release carrying them

Fix: strip the claim about WHY from the constant and move it to the per-fix line, which
is already per-route data. One brief, not two.

New opening:

```
Ship a release. These fixes have LANDED on `main` and the OS owes the fleet a release
carrying them:
```

And `ensure_release`'s `line` gains a reason suffix:

```python
reason = ("expedited by the filer" if issues.was_expedited(wo)
          else f"confirmed `{wo.get('issue_priority')}` by Neo")
line = (f"- {url} — fixed by `{wo['id']}`"
        + (f" ({wo['pr_url']})" if wo.get("pr_url") else "")
        + f" — {reason}")
```

A batch with both kinds in it then reads correctly, which a per-brief sentence could
never do. Expedited is checked first: an order can be both, and the user's scheduling
act is the one that is never contingent.

### 5. The red-`main` hold

Design fixed by Neo question 794. Not re-opened here.

**`ensure_release` ALWAYS creates or coalesces.** The batch must be durable and visible,
and `sync_issues` calls `ensure_release` only on the issue-state TRANSITION — a call
skipped because `main` was red never comes back, and the fix would be silently dropped.
So the hold is on DISPATCH, not on filing.

New daemon step `Daemon.hold_red_release(project, store)`, placed in the tick
IMMEDIATELY BEFORE `self.dispatch_pending(project, store, state)`. Before, not after:
`sync_issues` files the order late in tick N and `dispatch_pending` claims it early in
tick N+1, so anything running after dispatch holds nothing on the first opportunity.

Mechanism, in order — each guard is there to make the common case free:

1. `if project.name != self._os_owner(): return`. Same scope as
   `settle_shipped_releases`, same reason: the release path is `scripts/shipit.sh` and
   the production checkout, both facts about the OS's own repo. Every other project pays
   zero queries.
2. One indexed listing of `status='pending'` orders carrying `RELEASE_BATCH_KEY`. None
   (the overwhelming case) returns here, before any subprocess.
3. `if float(wo.get("retry_after") or 0) > db.now(): continue`. A hold already in force
   is the throttle: no `gh` call until it lapses. This is the whole rate limit and it
   needs no timer of its own.
4. `runs = ci.base_runs(base, cwd=project.path)`, cached per base ref for the step so
   two pending release orders cost one call — `_heal_inherited_red`'s pattern.
   `base = evidence.base_ref(project.path) or "main"`.
5. Red test — see below.
6. Red: `store.hold_dispatch(wo_id, until=max(existing_retry_after, db.now() +
   RED_HOLD_SECONDS))`, `RED_HOLD_SECONDS = 300`. `max(...)` so a red hold can never
   SHORTEN a launch-failure backoff written by `release_dispatch_claim`. Then the event
   (§5.2).
7. Green: clear the hold — `store.hold_dispatch(wo_id, until=None)` — **only when
   `dispatch_attempts == 0`**. A non-zero ladder belongs to `release_dispatch_claim` and
   is not this step's to erase. Green with no hold in force writes nothing.

`ProjectStore.hold_dispatch(wo_id: str, until: float | None)` is new and writes
`retry_after` AND NOTHING ELSE — explicitly not `dispatch_attempts`, whose only author
is `release_dispatch_claim`. `ProjectStore.claim_next_pending` guard 4 already refuses a
row with `COALESCE(retry_after, 0) > now`, so a held order simply is not claimed. No new
column, no new status: `pending` with a future `retry_after` is a state the claim query
already understands.

**Unreadable CI does NOT hold.** `ci.base_runs` raises `GitHubError` on any doubt; catch
it, `log.debug`, and leave the order claimable. Holding on an unreadable CI would strand
the user's expedited release for ever on a `gh` outage, and the fallback is real: the
brief still tells the worker to check `main` is green and stop, and `shipit.sh` is a
gated action a reviewer sees. Same direction `_heal_inherited_red` chose for the same
call.

#### 5.1 Which workflows

`ci.base_is_red(runs, workflows)` needs a workflow tuple; its only caller today passes
the workflows one pull request is failing. There is no pull request here.

Use **the workflows that reported on `main`'s newest head**:

```python
head = runs[0].head_sha if runs else ""
workflows = tuple(dict.fromkeys(
    r.workflow for r in runs if r.head_sha == head and r.workflow))
```

Why that set and not "every distinct workflow in `base_runs`": a workflow that ran once
months ago, failed, and was then deleted would latch `main` red for ever, because
`ci.latest` would keep returning that run. Scoping to the newest head asks only about
workflows that still exist.

Known and accepted drift: `ci.latest(runs, w)` returns the newest COMPLETED run of `w`,
which may be from an OLDER sha if the head's run is still in flight. That is the right
answer — it is the last thing `main` is known to have done, and treating an in-flight
head as green would be worse. `runs` empty (a repo with no CI) means not red.

#### 5.2 The event

`Daemon.RED_HOLD_EVENT = "release_held_red_base"`, payload
`{"base", "head_sha", "run_id", "workflow", "until", "detail"}`, on the RELEASE order.

**Dedupe key includes `head_sha`, not just the kind.** kn-7b122cd9 (the issue #793
post-mortem): a code per CONDITION is also a code per WORLD, and a key that omits the
thing that changes latches the first sentence for ever. `main` going red, being fixed,
and going red again on a different commit is two pieces of news. Follow
`_settle_shipped_release`'s `OVERTAKEN_EVENT`, which dedupes on the TAG:

```python
if any(db.from_json(e["payload"], {}).get("head_sha") == head
       for e in store.events_of_kind(wo_id, self.RED_HOLD_EVENT)):
    return
```

No attention flag. A red `main` for a few minutes is ordinary and must not read as a
fault — `_settle_shipped_release`'s rule for the same shape.

Renderer in `src/jarvis/timeline.py`, beside `release_overtaken`:

```python
if kind == "release_held_red_base":
    # On the RELEASE order: `main` is red, so the ship is deferred rather than
    # attempted. Deduped per head sha, so one line per broken commit.
    return ("Holding the release — the base branch is red",
            p.get("detail") or (p.get("base") or ""))
```

`detail` is built at write time: `f"{base} is red at {head_sha[:10]} ({workflow}) —
holding the release until it is green"`.

### 6. CLAUDE.md

One paragraph replaced, no new section: `evals/llm/test_jarvis_judgment.py:24` loads this
file as a bare system prompt and LLM-grades the operator persona, so operator content
stays first and dominant. Replace the `--expedite` paragraph of the `jarvis bug report`
entry — from `# --expedite is the ONE way to jump that` through `# work happens.` — with,
verbatim, preserving the column:

```
                                           # --expedite is the ONE way to jump that
                                           # queue, and it is a SCHEDULING decision, not
                                           # a rating: at ANY priority it files the issue
                                           # as usual AND dispatches a work order on it
                                           # immediately — the same thing `jarvis issues
                                           # start` would do, in one step, printed as the
                                           # wo-id. Use it so the priority can stay an
                                           # honest description of the defect instead of
                                           # being inflated to buy attention. AN
                                           # EXPEDITED FIX SHIPS A RELEASE WHEN IT LANDS,
                                           # at ANY priority and whatever Neo later says
                                           # about the rating: expediting IS the user
                                           # saying they want this in production now, so
                                           # making them ask again once it merges is
                                           # asking twice. Several landing close together
                                           # make ONE release, and a red `main` holds it
                                           # rather than shipping it. The rule that has
                                           # NOT changed is the other one: a CLAIMED
                                           # critical/blocker buys no release ON ITS OWN
                                           # — an unexpedited claim is dispatched only
                                           # once Neo confirms, and an expedited one is
                                           # dispatched carrying NO rating while the
                                           # re-assessment runs alongside. Neo's verdict
                                           # decides the RATING; the user's --expedite
                                           # decides the release. Don't cut one by hand.
```

### Tests

`tests/test_issue_lifecycle.py`, `fleet` fixture (`fleet.file_bug(expedite=True)`,
`fleet.land`, `fleet.releases`). `tests/test_release_overtaken.py` is what covers
`ensure_release`'s batching and `settle_shipped_releases` today — its
`_release_order`/`_join_batch` helpers build orders the way `ensure_release` does, and
its step-ordering test (the one asserting `"settle_shipped_releases"` sits in the tick's
step list) must gain `hold_red_release` before `dispatch_pending`.

**Existing tests that change — all four flip because the rule flipped:**

| Now | Becomes |
|---|---|
| `test_an_expedited_claim_that_lands_first_ships_no_release` | `test_an_expedited_claim_that_lands_first_still_ships_a_release` — asserts `len(fleet.releases()) == 1`, and that the order's `issue_priority` is still `""`: the release came from the flag, never from a rating |
| `test_a_claim_neo_could_not_be_asked_about_ships_no_release` | `test_an_expedited_claim_neo_could_not_be_asked_about_still_ships` — Neo unreachable fails closed on the RATING (label + `issue_priority` unchanged), not on the user's scheduling act |
| `test_a_verdict_that_arrives_after_the_fix_landed_ships_nothing` | `test_a_verdict_after_the_landing_adds_no_second_release` — exactly ONE release either way; the verdict still cannot reach back |
| `test_a_downgraded_expedited_fix_lands_without_a_release` | `test_a_downgraded_expedited_fix_still_ships` — the downgrade moves the label, not the flag |

`test_a_downgrade_cannot_undo_an_expedited_work_order` keeps its `issue_priority ==
"medium"` assertions and gains one: `metadata[EXPEDITED_KEY]` survives the downgrade.
`test_neo_confirming_an_expedited_claim_grants_it_the_release` keeps `len(...) == 1` —
both routes true must not cut two, which `ensure_release`'s url dedupe already
guarantees.

**New:**

1. `test_an_expedited_low_bug_that_lands_ships_a_release` — parametrised
   `low`/`medium`/`high`. The headline: a level `dispatches()` rejects, expedited, lands,
   one release order exists carrying the issue url.
2. `test_a_non_expedited_low_bug_that_lands_ships_nothing` — same levels via
   `jarvis issues start` rather than `--expedite`. Proves the flag and not the landing
   is what changed.
3. `test_two_expedited_fixes_landing_make_one_release` — two issues
   (`fleet.gh.next_issue`), both expedited, both landed; `len(fleet.releases()) == 1`
   and both urls in that order's `RELEASE_BATCH_KEY`, two `release_batched` events.
4. `test_an_expedited_order_records_that_it_was_expedited` — `metadata[EXPEDITED_KEY] is
   True` on the row at creation, and `False`/absent for a non-expedited filing.

`tests/test_release_overtaken.py`, new:

5. `test_a_red_main_holds_a_pending_release` — a pending release order, `ci.base_runs`
   faked red; after `hold_red_release`, `retry_after > db.now()`, `claim_next_pending`
   returns nothing, ONE `release_held_red_base` event, and its `detail` names the run.
6. `test_a_green_main_lets_the_release_go` — same order, green runs; `retry_after` is
   NULL, `claim_next_pending` returns it, no event.
7. `test_a_still_red_main_says_so_once_per_commit` — two sweeps at the same head sha
   write ONE event; a sweep at a NEW red head sha writes a second. The kn-7b122cd9 case.
8. `test_an_unreadable_ci_does_not_hold_the_release` — `base_runs` raises `GitHubError`;
   the order stays claimable.
9. `test_a_red_hold_never_shortens_a_dispatch_backoff` — an order already held by
   `release_dispatch_claim` with `dispatch_attempts > 0` keeps its later `retry_after`
   under red, and is NOT cleared by green.

## Rejected alternatives

**Write `issue_priority = "high"` (or any level) on an expedited order.** One line, no
new flag, reuses `dispatches`. Rejected: `issue_priority` is the RATING, it is what the
`priority:` label mirrors and what Neo settles, and forging it to buy a release is
exactly the priority inflation `--expedite` was built to stop
(`route_filing`'s docstring). It would also make `settle_work_order_priority` overwrite
the user's scheduling decision on the next Neo verdict.

**Trigger on `status == 'completed'` instead of the tracker close.** Rejected: an order
can complete with its code on an unmerged branch. Issue #232 separated those and
`sync_issues` is where the distinction is already correct.

**Skip `ensure_release` while `main` is red.** The obvious hold, and the one a reviewer
proposes. Rejected (Neo 794): `sync_issues` fires `ensure_release` only on the issue-state
transition, so a skipped call never returns and the fix is lost from every future batch.
File always, defer dispatch.

**A `held` status or a `release_hold_reason` column.** Rejected: `pending` +
`retry_after` is the deferral `claim_next_pending` guard 4 already honours, and every
surface that renders an order already handles `pending`. A new status would need a
renderer, a reconciler rule and a `doctor` check for a condition that lasts minutes.

**A per-condition hold code (`HOLD_RED_BASE`) deduped by kind.** Rejected explicitly:
kn-7b122cd9. Dedupe carries the head sha.

**Reuse `ops.record_base_health`.** Same shape, but it is keyed to a work order's own
pull request (`pr_url` in its payload, `invariants.base_red_note` reading it for the
status line) and a release order has no pull request. Overloading it would make that
status line say something false.

## Out of scope

- Any use of issue #793 / wo-8736a5c5 / PR #794's project-scoped red-base reading. That
  work is on `worktree-wo-8736a5c5` and NOT on `main`; this spec builds on
  `ci.base_runs` + `ci.base_is_red` as they exist. If #794 lands first, §5.1's workflow
  selection is the thing to revisit, not the hold.
- Making a `jarvis status` line out of the red hold. The event and renderer put it on the
  record; the status line is a follow-up.
- `jarvis wo` surface for the expedited flag beyond what `wo show` already renders from
  the brief.
- Any change to Neo triage, `settle_triage`, or the `priority:` label.

## Unresolved

- `RED_HOLD_SECONDS = 300` is a guess, not a measurement. It is only the re-check
  interval — a fixed `main` ships within five minutes — so it is cheap to be wrong about,
  and it is deliberately not a config key until someone wants a different number.
- A release order held red across a very long outage accumulates one event per red
  commit. Bounded by how often `main` breaks; no cap specified.
