# A merge held for running checks is not stuck

Work order wo-a1d7b87c. Fixes GitHub issue 1110. Fifth member of the exclude-and-discount
family, after the usage window, the validation CI hold
(`docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md`),
the in-flight turn (`.../2026-10-14-a-worker-turn-in-flight-is-not-stuck.md`), the red base
(`.../2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md`) and the unratified plan
(`.../2026-10-15-a-child-held-by-its-features-unratified-plan-is-not-stuck.md`).

Design is settled. This records the evidence, the shape and the reasoning; the boundary
question was decided by Neo on question 1992 of this work order.

## The problem

`ops.stuck_scan` (src/jarvis/ops.py:9856) excludes **exactly one CI wait, and it is gated on
`validating`** (src/jarvis/ops.py:9908-9915):

```python
        ci_held, ci_spans = False, []
        if wo["status"] == "validating":
            ...
                ci_held = cause == VALIDATION_CI_CAUSE and until > now
```

In `waiting_pr_merge` the only project fact the sweep reads is
`central_store.base_red_hold_spans` (src/jarvis/ops.py:9920-9921), the red-base pair. There
is no reader of **the auto-merge poll's own hold**.

So an order parked by that hold — the `automerge_held` event with code
`automerge.HELD_CHECKS_RUNNING` (`"checks_running"`, src/jarvis/automerge.py:115), written by
`Daemon._note_automerge_held` (src/jarvis/daemon.py:9561) because GitHub's checks on the
judged head have not finished (`automerge.decide`'s `pr.unfinished` arm,
src/jarvis/automerge.py:450-453) — gets:

* `excluded_cause=''`, so `stuck.assess` judges it;
* no discounted seconds, so the whole wait counts;
* a clock judged on **time in status** (`waiting_pr_merge` is not in
  `stuck.ACTIVITY_STATUSES`, src/jarvis/stuck.py:28) against the 1.0h default
  (`catalog.DEFAULT_FLEET_HEALTH_THRESHOLDS["waiting_pr_merge"] = 60`,
  src/jarvis/catalog.py:1081);

and buys an investigation — a full model session — for an order the OS itself decided not to
merge yet.

**Live case: wo-afcea414.** Validation passed round 1 on `7662d08b70`; an `automerge_held`
event with code `checks_running` on that same head; `unit (3.13)` still IN_PROGRESS on
PR #1108; swept at **61m** in `waiting_pr_merge`. Nothing was wrong with it.

The same row already says so on another surface: `jarvis wo show` renders
`auto_merge: held — CI has not finished on this commit — 1 check(s) still running` from
`ops.automerge_state`'s `line` (`ops._automerge_line`'s tail, src/jarvis/ops.py:4993), while
the sweep's blocker column prints `"nothing the record can name"` (src/jarvis/ops.py:10004).
One surface knows and the other spends a session — the 2026-10-12 defect verbatim, one hold
along.

### Root cause

`excluded_cause`'s hold family is **enumerated by hand**, and this hold was never added to
it. kn-55f258cd's general rule names this as the **fifth instance of one omission**: every
member of the family so far has been a self-clearing hold the OS writes down and the sweep
does not read.

Not a threshold problem. Raising `waiting_pr_merge` past CI's runtime stops reporting a pull
request nobody merged, which is the one thing the sweep exists for.

### Why this hold qualifies under kn-6e836793

The standing ruling's two tests, the same ones the 2026-10-12 spec reconciled against:

1. **The OS is deliberately not acting.** `_note_automerge_held` costs no round number, no
   seat, no gate request and — by its own docstring — is "DELIBERATELY NOT AN ATTENTION
   ITEM".
2. **The hold clears with no user decision in the loop.** The checks report, the next
   two-minute `poll_pull_requests` tick runs `decide` again, and the merge proposes itself.

## The fix

Five parts. The first two are the exclusion, the third is the liveness test it rests on, the
fourth is the discount — the pair is the whole point (kn-0a222b38) — and the fifth is the
blocker column.

### §1 The named cause and its sentence

**`src/jarvis/stuck.py`, beside `BASE_RED_CAUSE`** (src/jarvis/stuck.py:46-47):

```python
#: §1 of docs/superpowers/specs/2026-10-20-a-merge-held-for-running-checks-is-not-stuck.md.
AUTOMERGE_CHECKS_CAUSE = "GitHub's checks on the merge commit"
```

with **its own entry in `EXCLUSION_REASON`** (src/jarvis/stuck.py:56-76), on
`BASE_RED_CAUSE`'s wording model:

```python
    AUTOMERGE_CHECKS_CAUSE: ("the OS is holding this merge itself: GitHub has not finished "
                             "running the checks on the commit the panel judged, and "
                             "nothing merges a commit whose checks have not reported — it "
                             "clears when they do, with no decision from you, so nothing "
                             "is judged while that is true"),
```

`DEFAULT_EXCLUSION_REASON`'s "held by {cause}, which clears by itself" is **true** here —
unlike the plan hold, no user decision clears this — so the sentence is not a correction of a
false claim. It exists for `BASE_RED_CAUSE`'s reason: the default sentence says nothing a
reader can act on, and the reader must be able to tell **which** of the two
`waiting_pr_merge` holds has them, and that it is about **this commit's checks** rather than
the base branch's.

**It reaches `stuck.assess` as `excluded_cause`, and never as a `continue` in `stuck_scan`**
(kn-143a46df, kn-e91ff3a5). A `continue` has no unit test over the pure predicate and
produces no `Verdict` row, so the held order would vanish from `jarvis stuck` and `/stuck`
instead of appearing there with its reason. `stuck.assess` is unchanged apart from the
constant: its existing `elif excluded_cause:` branch (src/jarvis/stuck.py:172) composes the
sentence and still populates `active_seconds` and `threshold_seconds`, so the row shows the
arithmetic.

### §2 Precedence in `stuck_scan`'s `excluded_cause` chain

**After `stuck.BASE_RED_CAUSE`, before `stuck.LIVE_DEPENDENCY`** (src/jarvis/ops.py:9977-9985):

```python
            excluded_cause=(PAUSE_USAGE_LIMIT
                            if window or any(h.open for h in usage)
                            else VALIDATION_CI_CAUSE if ci_held
                            else stuck.BASE_RED_CAUSE
                            if base_red and wo["status"] == "waiting_pr_merge"
                            # AFTER the red base and BEFORE the dependency: both are
                            # `waiting_pr_merge`-only and CAN co-occur (§2).
                            else stuck.AUTOMERGE_CHECKS_CAUSE if automerge_checks_held
                            else stuck.LIVE_DEPENDENCY if live_edges
                            else stuck.PLAN_ASSUMPTION_CAUSE if plan_held else ""),
```

**The order is load-bearing, and the comment above it must say so.** Unlike the CI/base-red
pair, these two are **not** mutually exclusive by status: both are `waiting_pr_merge`-only,
and both can be true of one order at one instant. The red base is a **project** fact hoisted
by the caller from `CentralStore.base_health`, while the newest `automerge_held` row can
still carry `checks_running` from an earlier tick — the poll writes one event per
`(head_sha, code, reason, round)` and nothing rewrites it, so the record's newest hold lags
the fact by up to one poll.

Red base wins, on §2.4 of the plan-hold spec's ranking: **a fleet fact outranks a fact about
one order.** A broken default branch is the thing to go and look at; one pull request's
checks still running is not.

No collision with `VALIDATION_CI_CAUSE`, which is `validating`-only, and none with
`LIVE_DEPENDENCY` or `PLAN_ASSUMPTION_CAUSE`, which are `pending`-only. The existing comment
at src/jarvis/ops.py:9974-9976 claiming CI and base-red "cannot collide" stays true and gains
the sentence above.

### §3 The liveness test, which is the one already established in two places

A live hold is the **newest** `ops.automerge_state(store, wo)` row satisfying all three:

1. `state["kind"] == "automerge_held"`;
2. `not state.get("stale")`;
3. `str(state.get("code")) == automerge.HELD_CHECKS_RUNNING`.

Nothing new is derived. `automerge_state` (src/jarvis/ops.py:4026) already picks the newest
event by timestamp across `ops.AUTOMERGE_EVENTS`, already applies the sticky-refusal rule,
and already **marks a hold stale rather than dropping it**. The two precedents that read it
exactly this way, and which this must not diverge from:

* the `need("automerge")` arm of the rules facts pass (src/jarvis/ops.py:12409-12417):
  `state and state.get("kind") == "automerge_held" and not state.get("stale")` — "a stale
  hold is not a claim about NOW", so contributing a marked one would let a rule fire on
  history;
* `ops.force_validation_state` (src/jarvis/ops.py:6636): `if state.get("kind") !=
  "automerge_held" or state.get("stale"): state = {}` — a stale hold is treated as **no
  hold**.

A third reading of the same dict would be a third definition of "the OS is holding this
merge".

**`HELD_CHECKS_RUNNING` ONLY, and nothing wider** (kn-6e836793 — an extension stays an
extension only if its boundary is stated):

* `HELD_CHECKS_NONE` is "no check has reported on this commit"
  (src/jarvis/automerge.py:454). That is not a wait with an end; it may mean the workflow
  never fired, and an order sitting there forever is exactly what the sweep must surface.
* `HELD_CHECKS_FAILED` is **red** (src/jarvis/automerge.py:448). Nothing clears it but a
  push, which is somebody's work.
* `HELD_BASE_RED` is §2's sibling and already excluded by its own, better-sourced path (a
  fresh central fact, not an event that may lag).
* Every other code — `HELD_MERGE_STATE_UNCLEAN`, `HELD_SHA_MOVED`, `HELD_NOT_PASSED`,
  `HELD_ASSUMPTIONS`, `HELD_PLAN_ASSUMPTIONS`, `HELD_SHA_UNRECORDED`, `HELD_PR_CLOSED`,
  `HELD_NOT_MERGEABLE`, `HELD_BASE_MOVED` — names something a person or a worker owes, and
  several already have a `true_blockers` line of their own
  (src/jarvis/invariants.py, the `waiting_pr_merge` arms).
* A `needs_review` order beside the same pull request is a wait the **USER** owes and stays
  judged.

**Gated on `status == "waiting_pr_merge"`**, so no other order pays for the reads
(`true_blockers`' rule, restated at src/jarvis/ops.py:9962). `automerge_state` costs up to
five `events_of_kind` reads plus one `latest_validation_round`; one call per parked candidate
is the whole cost, and it is **one** call serving both the exclusion and §5's blocker note.

Nothing is hoisted by the caller and neither caller's signature changes. These are the
ORDER's own timeline rows, not a project fact — the opposite of the red base, where the
caller hoists because the fact is the project's.

### §4 The discount, and the pair is the whole point

`waiting_pr_merge` is judged on time in status, so an exclusion alone only **moves** the
false positive to the first sweep after the checks report: `seconds_in_status` is already
past 1.0h, nothing is held any more, and the sweep buys the investigation the exclusion
existed to prevent. kn-0a222b38's pair rule, and kn-6e30d561's corollary — **the discount
must outlive the exclusion by one status span.**

**A new pure spans function, shaped on `project_store.validation_ci_hold_spans`**
(src/jarvis/project_store.py:252): rows in, spans out, clipped to `[since, now]`, empties
dropped by `if end > start`.

```python
def automerge_checks_hold_spans(events: Iterable[Any], since: float,
                                now: float) -> list[tuple[float, float]]
```

**Where it lives: `src/jarvis/ops.py`, module level, directly below `AUTOMERGE_EVENTS` /
`AUTOMERGE_TERMINAL` (src/jarvis/ops.py:3919-3926) and above `automerge_state`.** Pure: no
store, no clock beyond its arguments, unit-testable over a list of dicts.

*Why there and not beside its two siblings.* `validation_ci_hold_spans`' docstring states the
rule as "the span derivation lives in the module that OWNS the rows, beside the cause
constant it filters on". The rows here are `automerge_*` timeline events and the vocabulary
that makes the walk correct — `AUTOMERGE_EVENTS` — is **ops'**. The alternatives were
weighed and lose:

* **`project_store.py`**, beside `validation_ci_hold_spans`: it sits BELOW `automerge` in the
  import layering — `automerge.py:52` does `from .project_store import validation_standing` —
  so filtering on `automerge.HELD_CHECKS_RUNNING` there means a back-import of a module that
  imports `project_store`. The layering has no such cycle today and does not gain one for a
  constant.
* **`automerge.py`**, beside the code it filters on: the walk needs `ops.AUTOMERGE_EVENTS`,
  and `automerge` cannot import `ops`.
* **`central_store.py`**: wrong rows. It owns the fleet's `os_state`, not an order's
  timeline.
* **`holds.py`**: `union_seconds`' rule — "pure arithmetic over pairs: no `Hold`, no cause,
  no clock" — and §6's rejected alternative for `_episodes`.
* **`stuck.py`**: its opening paragraph forbids anything that knows a row shape.

**THE BOUNDARY, settled by Neo on question 1992 of this work order, option (a):** each
`checks_running` `automerge_held` event's span runs from **its own `ts`** to the `ts` of the
**NEXT event of any kind in `ops.AUTOMERGE_EVENTS`**, and **a hold with no successor
contributes NOTHING.**

The reasoning, which must stay on the record because the two siblings both do the opposite:

* **There is no `reopens_at` and no refresh.** `Daemon._validation_ci_held` rewrites its
  event every `CI_HOLD_RECHECK_SECONDS`, which is what lets `validation_ci_hold_spans` end a
  span at a stored timestamp. `_note_automerge_held` writes **one** event per
  `(head_sha, code, reason, round)` and nothing rewrites it, so the record carries a start
  and no end.
* **A hold that lapsed because the checks went GREEN is superseded by a different kind** —
  `automerge_proposed`, then `automerge_decided`/`automerge_merged` — not by another hold. So
  the next event of **any** `AUTOMERGE_EVENTS` kind is the earliest moment the record can
  prove the hold had ended, and that is precisely what option (a) ends the span at.
* **An open tail to `now` would discount a wait the merge GATE owns.** After the checks pass,
  the order sits in `waiting_pr_merge` waiting on a gate request — a wait somebody may well
  owe — and a growing span would forgive it under the checks' name.
* **And it would break `base_red_hold_spans`' stated rule** (src/jarvis/central_store.py:71-73):
  an open span must never end at `now` on a fact nobody can confirm. A broken `gh` writes no
  new event at all; `Daemon._warn_pr_poll_broken` (src/jarvis/daemon.py:10849) is what
  surfaces that, not a silently growing discount.

Under (a) the open window is carried by the **EXCLUSION** (§2-§3, which reads the live hold)
and the discount accrues only over a window a later event confirms has ended. The two halves
degrade the same way: a `gh` that stopped answering freezes both — no new hold to exclude on,
no successor to close a span with — and every failure therefore makes the sweep judge MORE
time, never less. That is kn-6e30d561's required direction.

**The caller must re-sort, not filter.** `ProjectStore.events_of_kind` takes ONE kind and the
walk is only right in `ts` order, so `stuck_scan` concatenates every `AUTOMERGE_EVENTS` kind
and sorts — `plan_hold_spans`' concatenate-and-re-sort shape (src/jarvis/ops.py:9940-9945),
for the same stated reason:

```python
        automerge_checks_held, automerge_spans = False, []
        if wo["status"] == "waiting_pr_merge":
            am_note = invariants.automerge_checks_note(automerge_state(pstore, wo))
            automerge_checks_held = bool(am_note)
            automerge_spans = automerge_checks_hold_spans(
                sorted((e for k in AUTOMERGE_EVENTS
                        for e in pstore.events_of_kind(wo["id"], k)),
                       key=lambda e: float(e["ts"])), since, now)
```

The whole history is walked and the SPANS are clipped, never the rows —
`plan_hold_spans`' rule: a hold that opened before the current status span is the normal
case, and filtering rows by `since` first would lose it.

**Joined to the existing union, never summed** (src/jarvis/ops.py:9948-9950):

```python
        discounted = holds.union_seconds(
            [(max(h.started, since), min(h.finish(now), now)) for h in usage]
            + ci_spans + base_red_spans + plan_spans + automerge_spans)
```

`holds.union_seconds`' own rule: a usage window can shut while the checks run, and a red base
can overlap the same minutes — summing would tell a reader the same minutes twice under two
names.

`automerge_spans` is `[]` for every status but `waiting_pr_merge`, for §3's reason.

### §5 The blocker column

`stuck_scan`'s blocker chain prints `"nothing the record can name"`
(src/jarvis/ops.py:10000-10004) on the same row where `jarvis wo show` prints
`auto_merge: held — CI has not finished on this commit — N check(s) still running`. §1(2) of
the 2026-10-15 plan-hold spec is this exact defect, and its medicine is **one shared helper**
(`invariants.plan_hold_note`, src/jarvis/invariants.py:1943, "ONE HOME, TWO READERS").

**Where it lives: `src/jarvis/invariants.py`, beside `base_red_note` (line 1892) and
`plan_hold_note` (line 1943)**:

```python
def automerge_checks_note(state: dict[str, Any] | None) -> str:
    """One sentence for a LIVE `checks_running` auto-merge hold, `""` for anything else."""
```

Pure, over the dict `ops.automerge_state` returns (or `None`). It performs §3's triple test
itself — kind, not stale, `code == automerge.HELD_CHECKS_RUNNING`, the constant reached by a
function-local `from .automerge import HELD_CHECKS_RUNNING`, `base_red_note`'s
function-local-import shape — and returns the blocker sentence built from the event's own
stored `reason` plus the short head sha, e.g.

```
waiting for GitHub's checks on 7662d08b70 — CI has not finished on this commit — 1 check(s)
still running
```

*Why it lives in `invariants.py` and takes the dict.* `base_red_note` is the shape precedent:
a note derived from the newest event, with **no network** — "whether the base is red is a
question for GitHub, and this module may not ask it". Same division here: the poll wrote the
hold down and this reads it back. Taking the dict rather than a store keeps `invariants` free
of an `ops` import (`ops` imports `invariants`), exactly as `plan_hold_note` takes a row.

**Its readers, stated exactly.** TWO, and both inside `ops.stuck_scan`:

1. the blocker column (`blocker[0] or outstanding_children_note or plan_hold_note or
   automerge_checks_note or "nothing the record can name"` — appended after the plan-hold
   note, which is `pending`-only and cannot co-occur);
2. the §2 **predicate**: `automerge_checks_held = bool(am_note)`.

That second reader is the design and not a shortcut. Two functions over the same dict — one
deciding the exclusion, one wording the row — is how the sweep's two columns came to
disagree in the first place; `bool(note)` makes "excluded" and "the blocker says why"
inseparable by construction.

**It is NOT wired into `invariants.status_label`**, and that is a decision. `status_label`
runs per row in every listing, and its `PR_REPAIR_STATUSES` arm pays "ONE INDEXED READ"
(`base_red_note`'s stated budget); `automerge_state` costs up to six. The sweep is gated on
`waiting_pr_merge` and pays it once. Naming this, so the next reader does not take the
absence for an omission: a parked order's listing label is unchanged by this spec.

**The helper is FLAGLESS.** `invariants.true_blockers` gets **no** branch, so
INV-ATTENTION-MISSING (`check_blocked_work_is_surfaced`) stays quiet with no exception added
for it — kn-143a46df's load-bearing rule, and the same construction `status_label`'s
precondition and plan-hold arms call out ("Flagless like the three below: `true_blockers`
gets no branch, so INV-ATTENTION-MISSING stays quiet without an exception"). A held
auto-merge is ordinary (`_note_automerge_held`: "DELIBERATELY NOT AN ATTENTION ITEM"); this
spec changes who gets INVESTIGATED, not who gets flagged.

### §6 Surfaces: prose only

No new renderer. `ops.stuck_report` already carries `excluded` and `reason` on every row,
`cli.cmd_stuck` prints `row["reason"]`, and `stuck.html:62` renders `{{ r.excluded or '—' }}`.
Three prose edits, each of which becomes **false** the moment §1 ships:

1. `src/jarvis/ui/templates/stuck.html:42-49` enumerates the holds — "...and a merge the OS
   is holding itself because the default branch is red" — and must name this one too, or the
   page lists a cause its own legend denies.
2. `stuck.assess`'s docstring (src/jarvis/stuck.py:134-152) enumerates the same set and says
   "all five resume by themselves". Add this cause and keep the kn-6e836793 reconciliation
   paragraph accurate.
3. `stuck.py`'s `EXCLUSION_REASON` preamble (src/jarvis/stuck.py:53-55) says `{cause}` and
   `{kind}` are the only slots — still true; the new sentence takes no slot.

### §7 Test plan

`tests/test_health_sweep.py` owns the sweep; `tests/test_validation_ci_hold.py` is the pair's
precedent; `tests/test_automerge.py` owns the hold's own lifecycle. Extend those three — no
new file.

In `tests/test_health_sweep.py`:

1. **Held and excluded with the named cause.** `waiting_pr_merge` for 61m (wo-afcea414's
   number), newest event an `automerge_held` with `code="checks_running"` on the judged head:
   `verdict.stuck is False`, `verdict.excluded == stuck.AUTOMERGE_CHECKS_CAUSE`, and
   `verdict.reason == EXCLUSION_REASON[AUTOMERGE_CHECKS_CAUSE]` rather than the default
   sentence.
2. **The blocker column names it.** Same row: `row["blocker"]` is the
   `automerge_checks_note` sentence and **not** `"nothing the record can name"`. The §5
   regression guard, asserted through the sweep and not the helper.
3. **Every other code is still judged.** `checks_none`, `checks_failed`,
   `merge_state_unclean` and `sha_moved` at 61m: `stuck is True`, `excluded == ""`. Four
   cases, because §3's boundary is the part a later change will widen by accident.
4. **A stale hold is no hold.** A `checks_running` hold `automerge_state` marks `stale`
   (order no longer in `waiting_pr_merge`'s round, `_automerge_hold_is_stale`'s cases):
   `stuck is True`, `excluded == ""`.
5. **`needs_review` beside the same pull request is still judged.** Same events, status
   `needs_review`: `stuck is True`, `excluded == ""`. kn-6e836793's scoping guard.
6. **Precedence: a red base outranks it.** Both true — fresh red central fact AND a live
   `checks_running` hold: `excluded == stuck.BASE_RED_CAUSE`. The §2 assertion, and the test
   that fails if the new arm is inserted above the base-red one.
7. **The checks reporting leaves the closed span discounted.** The kn-6e30d561 case and the
   reason the discount exists: 70m in `waiting_pr_merge`, a `checks_running` hold at
   `since + 2m` followed by an `automerge_proposed` at `since + 65m`, no live hold now.
   `excluded == ""` and `stuck is False`, because `active_seconds ≈ 7m` is inside the 1.0h
   threshold. **Assert `active_seconds`** — the number is the claim.
8. **Union, not sum.** A usage hold fully inside the discounted span: `row["discounted"]`
   equals the union, strictly less than the total.
9. **No `continue`.** With `stuck_only=False` the held order is PRESENT in `stuck_scan`'s
   rows and in `ops.stuck_report`, carrying `excluded` — kn-143a46df's structural guard.
10. **Both callers agree.** `Daemon._stuck_candidates` and `ops.stuck_report` over the same
    store and clock produce the same verdict for cases 1 and 3; the §7 "one derivation"
    invariant of the 2026-09-30 spec.

In `tests/test_automerge.py` (or beside `plan_hold_spans`' units, whichever the repo's
convention puts the pure functions in):

11. **`automerge_checks_hold_spans` as a pure unit:** a hold closed by a later
    `automerge_proposed`; a hold closed by a later `automerge_held` of a different code; two
    holds in sequence; a hold with **no** successor yielding **NOTHING** (Neo 1992's option
    (a), the assertion that fails if an open tail to `now` is ever added); a span wholly
    before `since` yielding nothing; clipping at `since` and at `now`; rows handed in out of
    `ts` order still walked correctly.
12. **`invariants.automerge_checks_note` as a pure unit:** `None`, a non-hold kind, a stale
    hold, a hold with each other code — all `""`; a live `checks_running` hold quoting the
    stored `reason` and the short sha.
13. **Flagless.** `invariants.true_blockers` on the held order returns the same list it
    returns today, and `check_blocked_work_is_surfaced` raises no violation for it.

### §8 Rejected alternatives

1. **Synthesise the hold inside `holds._episodes`** — the cheap fix, and the one a reviewer
   proposes, since `holds` already models held spans. REJECTED, and kn-0a222b38 records it as
   rejected with the blast radius: `holds.held` feeds `jarvis inspect`,
   `Daemon.check_burning_turns`, the alarms and the feature rollups, all of which would
   silently start subtracting merge-wait time from wall clock. A turn that ran for two hours
   did run for two hours. `holds` also clips to turns, so a span on a parked order with no
   live turn would be zeroed. Out of scope here by instruction as well as by argument.
2. **Raise the `waiting_pr_merge` threshold.** Treats the symptom and buys a second bug: a
   pull request nobody merged for six hours with green checks is precisely what the sweep
   must report. The wait is not slow, it is **held**, and those are different facts.
3. **Exclude on any `automerge_held`.** One line shorter and wrong in four directions — §3
   enumerates them. `checks_failed` is red and `checks_none` may mean the workflow never
   fired; forgiving either hides work somebody owes.
4. **End an open span at `now`.** Neo 1992's option (b). It would discount a wait the merge
   gate owns, and would grow on a fact nobody can confirm when `gh` stops answering — the
   over-discount direction kn-6e30d561 forbids, and the rule `base_red_hold_spans` already
   states for its own open pairs.
5. **A `reopens_at` on the `automerge_held` payload**, making this hold look like the
   validation CI one. That is a change to the poll's write path and to the dedupe key, with
   `ops._automerge_hold_is_stale` and `force_validation_state` reading the same payload; the
   successor event already dates the end, so the write path earns nothing.
6. **A `fleet_health.*` catalog key** for the merge wait. A setting asks the user to know
   GitHub's runtime. The hold and its successor are already in the record; reading them is
   free.
7. **A `continue` in `stuck_scan`.** kn-143a46df, kn-e91ff3a5 — §1.
8. **Name the blocker in `invariants.true_blockers`.** kn-143a46df: a blocker named there
   arms INV-ATTENTION-MISSING, and the deliberate silence is correct. The fix is to not open
   the investigation.

### §9 Out of scope, explicitly

* **`holds.py` is untouched** — no `_episodes` synthesis, no new `HOLD_CAUSES` entry (§8.1).
* **No threshold changes.** `waiting_pr_merge` stays 60 minutes; `fleet_health.cooldown_minutes`,
  `max_per_day` and the fingerprint are untouched. The repeat-per-episode symptom is cured by
  not flagging the order at all.
* **No catalog key** of any kind.
* **No change to `Daemon._note_automerge_held`, its dedupe key, `automerge.decide` or any
  `HELD_*` code.** The record already says the right thing; the defect was that the sweep did
  not read it.
* **`ops.automerge_state`, `ops._automerge_line` and `jarvis wo show` are unchanged.** They
  were the surface that was already correct.
* **`invariants.status_label` gains nothing** — §5, with the read-budget reason.
* **No new notification and no attention flag** — §5, flagless.
* **The other `HELD_*` codes gain no exclusion.** Naming that here is what keeps the next
  extension from being assumed (the 2026-10-12 spec's closing rule).
