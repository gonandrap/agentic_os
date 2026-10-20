# A merge held for running checks is not stuck

Work order wo-a1d7b87c. Fixes GitHub issue 1110. Fifth member of the exclude-and-discount
family, after the usage window, the validation CI hold
(`docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md`),
the in-flight turn (`.../2026-10-14-a-worker-turn-in-flight-is-not-stuck.md`), the red base
(`.../2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md`) and the unratified plan
(`.../2026-10-15-a-child-held-by-its-features-unratified-plan-is-not-stuck.md`).

Design is settled. This records the evidence, the shape and the reasoning. The boundary
question was decided by Neo on question 1992 of this work order and **amended by Neo on
question 1998**, which supersedes 1992's option (a) in one respect — §4 records both.

## The problem

`ops.stuck_scan` (src/jarvis/ops.py:9913) excludes **exactly one CI wait, and it is gated on
`validating`** (src/jarvis/ops.py:9908-9915):

```python
        ci_held, ci_spans = False, []
        if wo["status"] == "validating":
            ...
                ci_held = cause == VALIDATION_CI_CAUSE and until > now
```

In `waiting_pr_merge` the only project fact the sweep reads is
`central_store.base_red_hold_spans` (src/jarvis/ops.py:9975), the red-base pair. There
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
the fact by up to one poll. §3's fourth condition is what bounds that lag.

Red base wins, on §2.4 of the plan-hold spec's ranking: **a fleet fact outranks a fact about
one order.** A broken default branch is the thing to go and look at; one pull request's
checks still running is not.

No collision with `VALIDATION_CI_CAUSE`, which is `validating`-only, and none with
`LIVE_DEPENDENCY` or `PLAN_ASSUMPTION_CAUSE`, which are `pending`-only. The existing comment
at src/jarvis/ops.py:9974-9976 claiming CI and base-red "cannot collide" stays true and gains
the sentence above.

### §3 The liveness test: the established triple, plus a freshness horizon

A live hold is the **newest** `ops.automerge_state(store, wo)` row satisfying all three:

1. `state["kind"] == "automerge_held"`;
2. `not state.get("stale")`;
3. `str(state.get("code")) == automerge.HELD_CHECKS_RUNNING`;

**AND a fourth condition, on the project, decided by Neo on question 1998:**

4. the project's `CentralStore.base_health` fact is **FRESH**, expressed as
   `base_red_until >= now`.

**The horizon is `ops._base_red_until(fact, now)`** (src/jarvis/ops.py:5490), which is
`min(now, checked_at + central_store.BASE_HEALTH_FRESH_SECONDS)` — 900s
(src/jarvis/central_store.py:22). Freshness is expressible as `horizon >= now` **because the
horizon is a `min(now, …)`**: a stale fact clips strictly below `now`, a fresh one lands
exactly on it.

**It is ALREADY a parameter and nothing new is plumbed.** `stuck_scan` takes
`base_red_until: float = 0.0` (src/jarvis/ops.py:9917), both callers hoist it once per
project — `ops.stuck_report` at src/jarvis/ops.py:10117-10120 and `Daemon._stuck_candidates`
at src/jarvis/daemon.py:5678-5688 — and `base_red_spans` already consumes it
(src/jarvis/ops.py:9975). **No signature change, no rename.** The name predates this second
reader; renaming it would touch two callers and the sweep's tests to buy nothing. Only
`stuck_scan`'s docstring gains a sentence saying the horizon now serves both
`waiting_pr_merge` holds.

**Why THIS fact and no new record.** The hoist already happens under
`spec.validation.enabled and spec.validation.auto_merge` (src/jarvis/ops.py:10117) — exactly
the projects where a `checks_running` hold can exist, because nothing else opens one.
`Daemon.poll_default_branch` stamps `checked_at` every tick for those same projects and
writes **NOTHING** on `github.GitHubError` (src/jarvis/daemon.py:7428-7443), so the horizon is
the OS's own most recent confirmed observation of GitHub for that project. No schema change
and no new per-tick write.

**Degradation is the correct one in both of its cases.** The horizon is `0.0` when
`checked_at` is absent or non-numeric, and the caller leaves it `0.0` when the project is not
opted into auto-merge. `0.0 >= now` is false, so the liveness test fails and §4's `min`
clips every span away: an order whose project the OS is not observing is **judged**, not
forgiven.

Nothing new is derived from the timeline. `automerge_state` (src/jarvis/ops.py:4026) already
picks the newest event by timestamp across `ops.AUTOMERGE_EVENTS`, already applies the
sticky-refusal rule, and already **marks a hold stale rather than dropping it**. The two
precedents that read it exactly this way, and which the triple must not diverge from:

* the `need("automerge")` arm of the rules facts pass (src/jarvis/ops.py:12409-12417):
  `state and state.get("kind") == "automerge_held" and not state.get("stale")` — "a stale
  hold is not a claim about NOW", so contributing a marked one would let a rule fire on
  history;
* `ops.force_validation_state` (src/jarvis/ops.py:6636): `if state.get("kind") !=
  "automerge_held" or state.get("stale"): state = {}` — a stale hold is treated as **no
  hold**.

A third reading of the same dict would be a third definition of "the OS is holding this
merge".

**Where the fourth condition is applied, and where it is NOT.** The predicate is
`automerge_checks_held = bool(am_note) and base_red_until >= now` in `stuck_scan`.
`invariants.automerge_checks_note` (§5) stays **pure over the dict and tests only the
triple**: what the record SAYS is timeless, while liveness is a claim about `now` resting on
a hoisted project fact the helper may not reach. So §5's inseparability is a one-way
implication and must be read as such — **an exclusion always comes with a named blocker; a
named blocker does not always exclude.** A held order whose project's fact has gone stale is
judged, and its blocker column still says what the record knows, so the investigation it buys
starts informed instead of on `"nothing the record can name"`.

**`HELD_CHECKS_RUNNING` ONLY, and nothing wider** (kn-6e836793 — an extension stays an
extension only if its boundary is stated):

* `HELD_CHECKS_NONE` is "no check has reported on this commit"
  (src/jarvis/automerge.py:454). That is not a wait with an end; it may mean the workflow
  never fired, and an order sitting there forever is exactly what the sweep must surface.
* `HELD_CHECKS_FAILED` is **red** (src/jarvis/automerge.py:448). Nothing clears it but a
  push, which is somebody's work.
* `HELD_BASE_RED` is §2's sibling and already excluded by its own, better-sourced path (the
  central fact itself, not an event that may lag).
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

Nothing else is hoisted and neither caller's signature changes. The timeline rows are the
ORDER's own — the opposite of the red base and of the horizon, both of which are the
project's and therefore hoisted.

### §4 The discount, and the pair is the whole point

`waiting_pr_merge` is judged on time in status, so an exclusion alone only **moves** the
false positive to the first sweep after the checks report: `seconds_in_status` is already
past 1.0h, nothing is held any more, and the sweep buys the investigation the exclusion
existed to prevent. kn-0a222b38's pair rule, and kn-6e30d561's corollary — **the discount
must outlive the exclusion by one status span.**

**A new pure spans function, shaped on `central_store.base_red_hold_spans`**
(src/jarvis/central_store.py:62): rows in, spans out, clipped to `[since, now]`, empties
dropped by `if end > start`, and an **open** span ended by a horizon passed in — the same
fourth positional argument, under the same name:

```python
def automerge_checks_hold_spans(events: Iterable[Any], since: float, now: float,
                                open_end: float) -> list[tuple[float, float]]
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

**THE BOUNDARY.** Each `checks_running` `automerge_held` event's span runs from **its own
`ts`** to

```
min(ts of the NEXT event of any kind in ops.AUTOMERGE_EVENTS, open_end, now)
```

where `open_end` is §3's horizon and a hold with no successor therefore ends at the horizon.
Empty results are dropped, so a stale fact (`open_end` below the hold's own start, `0.0` in
the limit) contributes nothing at all.

**This SUPERSEDES Neo question 1992's option (a) in one respect, and the record says so
rather than leaving the next reader to find a contradiction.** 1992 ruled that a hold with
**no successor contributes NOTHING**; question 1998 replaces that with the horizon. The
reason: 1992 was answered with no bound available, and "contributes nothing" was the only
safe degradation for an open span nobody could close. With a horizon in hand it is no longer
the right one — it under-discounts every live hold, which is the half of the pair that must
outlive the exclusion. The 1992 rule survives as the **limit case**: with no fresh fact the
horizon clips the span away and contributes nothing, exactly as before.

The rest of 1992's reasoning stands, and must stay on the record because the two siblings
both do the opposite:

* **There is no `reopens_at` and no refresh.** `Daemon._validation_ci_held` rewrites its
  event every `CI_HOLD_RECHECK_SECONDS`, which is what lets `validation_ci_hold_spans` end a
  span at a stored timestamp. `_note_automerge_held` writes **one** event per
  `(head_sha, code, reason, round)` and nothing rewrites it, so the record carries a start
  and no end.
* **A hold that lapsed because the checks went GREEN is superseded by a different kind** —
  `automerge_proposed`, then `automerge_decided`/`automerge_merged` — not by another hold. So
  the next event of **any** `AUTOMERGE_EVENTS` kind is the earliest moment the record can
  prove the hold had ended, and the `min` prefers it whenever it exists.

**Why the horizon and NEVER `now`** — the four reasons an open tail to `now` is wrong are
unchanged, and they are what the horizon answers:

1. After the checks pass, the order sits in `waiting_pr_merge` waiting on a gate request — a
   wait somebody may well owe — and a tail to `now` would forgive it under the checks' name.
2. `base_red_hold_spans`' stated rule (src/jarvis/central_store.py:71-73): an open span must
   never end at `now` on a fact nobody can confirm.
3. A broken `gh` writes no new event at all. `Daemon._warn_pr_poll_broken`
   (src/jarvis/daemon.py:10849) is what surfaces that, not a silently growing discount.
4. kn-6e30d561's required direction: every failure to observe must make the sweep judge more
   time, never less.

**What a `gh` outage now does, stated so the code docstring and kn-09ccf1c7 can match it word
for word.** The horizon is what makes reason 4 true, and without it both halves
over-forgave — the live hold stayed excluded for the whole outage, and the poll's first event
on recovery became the successor, discounting the entire gap:

> **A `gh` that stops answering stops BOTH halves forgiving within one
> `BASE_HEALTH_FRESH_SECONDS` window of the last confirmed observation: the exclusion lapses
> because the base-health fact is no longer fresh, and every open span ends at that same
> horizon rather than at the outage's end — so a longer outage makes the sweep judge MORE
> time, never less.**

This is the doctrine of kn-2579843f (Neo question 1818) applied verbatim: "a stale fact
excludes nothing" and "a stale fact stops discounting" are **ONE rule**, and an open pair ends
at the freshness horizon and NEVER at `now`. That entry is the authority for this section.

**The function owns the sort; the caller only concatenates.** `ProjectStore.events_of_kind`
takes ONE kind and the walk is only right in `ts` order, so `stuck_scan` concatenates every
`AUTOMERGE_EVENTS` kind and hands them over unordered — `automerge_checks_hold_spans` sorts
the rows itself ("THE ROWS ARE SORTED HERE AND NOWHERE ELSE"), so no caller sorts them a
second time:

```python
        automerge_checks_held, automerge_spans = False, []
        if wo["status"] == "waiting_pr_merge":
            am_note = invariants.automerge_checks_note(automerge_state(pstore, wo))
            automerge_checks_held = bool(am_note) and base_red_until >= now
            automerge_spans = automerge_checks_hold_spans(
                [e for k in AUTOMERGE_EVENTS
                 for e in pstore.events_of_kind(wo["id"], k)],
                since, now, base_red_until)
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
    """One sentence for a `checks_running` auto-merge hold, `""` for anything else."""
```

Pure, over the dict `ops.automerge_state` returns (or `None`). It performs §3's **triple**
test itself — kind, not stale, `code == automerge.HELD_CHECKS_RUNNING`, the constant reached
by a function-local `from .automerge import HELD_CHECKS_RUNNING`, `base_red_note`'s
function-local-import shape — and returns the blocker sentence built from the event's own
stored `reason` plus the short head sha, e.g.

```
waiting for GitHub's checks on 7662d08b70 — CI has not finished on this commit — 1 check(s)
still running
```

It does **not** test §3's fourth condition, for §3's stated reason: the horizon is a project
fact the helper cannot reach, and the sentence describes the record rather than claiming a
hold is live now.

*Why it lives in `invariants.py` and takes the dict.* `base_red_note` is the shape precedent:
a note derived from the newest event, with **no network** — "whether the base is red is a
question for GitHub, and this module may not ask it". Same division here: the poll wrote the
hold down and this reads it back. Taking the dict rather than a store keeps `invariants` free
of an `ops` import (`ops` imports `invariants`), exactly as `plan_hold_note` takes a row.

**Its readers, stated exactly.** TWO, and both inside `ops.stuck_scan`:

1. the blocker column (`blocker[0] or outstanding_children_note or plan_hold_note or
   automerge_checks_note or "nothing the record can name"` — appended after the plan-hold
   note, which is `pending`-only and cannot co-occur);
2. the §2 **predicate**, as the first conjunct: `bool(am_note) and base_red_until >= now`.

That second reader is the design and not a shortcut. Two functions over the same dict — one
deciding the exclusion, one wording the row — is how the sweep's two columns came to
disagree in the first place; sharing the note makes "excluded" and "the blocker says why"
inseparable **in that direction**: an exclusion cannot happen without a named blocker.

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

A fourth text is not a surface but must agree with §4's outage sentence word for word:
`automerge_checks_hold_spans`' own docstring. Knowledge entry kn-09ccf1c7 is corrected by the
same words, outside this repo.

### §7 Test plan

`tests/test_health_sweep.py` owns the sweep; `tests/test_validation_ci_hold.py` is the pair's
precedent; `tests/test_automerge.py` owns the hold's own lifecycle. Extend those three — no
new file. Every sweep case below hands `stuck_scan` a FRESH `base_red_until` (`now`) unless
it says otherwise.

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
8. **A STALE base-health fact un-excludes AND stops discounting.** Case 1's row, except
   `base_red_until = now - 1`: `excluded == ""`, `stuck is True`, and `row["discounted"]` has
   no second from the open hold. The §3/§4 guard, and the test that fails if either half of
   kn-2579843f's one rule is implemented without the other. A second assertion with
   `base_red_until = 0.0` (no `checked_at`, or a project not opted into auto-merge) for the
   same outcome.
9. **An open hold IS discounted while the fact is fresh, up to the horizon only.** 61m in
   status, a `checks_running` hold at `since + 1m` and no successor, `base_red_until`
   stamped `BASE_HEALTH_FRESH_SECONDS` into the span: `row["discounted"]` equals
   `horizon - (since + 1m)` and **not** `now - (since + 1m)`. The assertion that fails under
   both the superseded 1992 rule (which gives `0`) and an open tail to `now`.
10. **Union, not sum.** A usage hold fully inside the discounted span: `row["discounted"]`
    equals the union, strictly less than the total.
11. **No `continue`.** With `stuck_only=False` the held order is PRESENT in `stuck_scan`'s
    rows and in `ops.stuck_report`, carrying `excluded` — kn-143a46df's structural guard.
12. **Both callers agree.** `Daemon._stuck_candidates` and `ops.stuck_report` over the same
    store and clock produce the same verdict for cases 1, 3 and 8; the §7 "one derivation"
    invariant of the 2026-09-30 spec. Case 8 is in the list because the horizon is hoisted
    separately in each caller.

In `tests/test_automerge.py` (or beside `plan_hold_spans`' units, whichever the repo's
convention puts the pure functions in):

13. **`automerge_checks_hold_spans` as a pure unit:** a hold closed by a later
    `automerge_proposed`; a hold closed by a later `automerge_held` of a different code; two
    holds in sequence; a hold with **no** successor ending at `open_end`; the same hold with
    `open_end = 0.0` yielding **nothing**; a successor LATER than `open_end` (the span ends at
    `open_end`) and one EARLIER (it ends at the successor) — the `min`'s two arms; a span
    wholly before `since` yielding nothing; clipping at `since` and at `now`; rows handed in
    out of `ts` order still walked correctly.
14. **`invariants.automerge_checks_note` as a pure unit:** `None`, a non-hold kind, a stale
    hold, a hold with each other code — all `""`; a live `checks_running` hold quoting the
    stored `reason` and the short sha. No horizon argument — §5.
15. **Flagless.** `invariants.true_blockers` on the held order returns the same list it
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
   states for its own open pairs. §4's four reasons.
5. **No time bound at all** — the triple with no fourth condition, which is what review round
   1 rejected and correctly so. The hold stays excluded for the whole outage, and the poll's
   first event on recovery closes a span over the entire gap. Both halves forgive time nobody
   observed; kn-2579843f forbids each of them separately.
6. **A new per-tick work-order column stamped by the merge poll** — the most precise bound
   available, since it would be a confirmed observation of THIS pull request rather than of
   the project. Rejected on cost: a schema change plus a write on the poll's green path,
   which that poll's stated budget avoids writing on. The project-level fact is one
   observation coarser and free.
7. **A fixed maximum hold age, no new record** — treat a `checks_running` hold older than N
   as dead. Rejected twice over: N is an arbitrary constant with nothing to derive it from,
   and it un-excludes a legitimately held order whose checks are merely slow — which is the
   defect this spec exists to fix, reintroduced on a timer.
8. **Document the gap as accepted** — the reviewer's second option: keep the unbounded
   exclusion and write down that an outage over-forgives. Rejected: kn-2579843f is a standing
   rule, not a preference, and a bound was available at no cost. Accepting a gap is for the
   case where the alternative is expensive; here it was a parameter the function already
   took.
9. **A `reopens_at` on the `automerge_held` payload**, making this hold look like the
   validation CI one. That is a change to the poll's write path and to the dedupe key, with
   `ops._automerge_hold_is_stale` and `force_validation_state` reading the same payload; the
   successor event and the horizon already date the end, so the write path earns nothing.
10. **A `fleet_health.*` catalog key** for the merge wait. A setting asks the user to know
    GitHub's runtime. The hold, its successor and the horizon are already in the record;
    reading them is free.
11. **A `continue` in `stuck_scan`.** kn-143a46df, kn-e91ff3a5 — §1.
12. **Name the blocker in `invariants.true_blockers`.** kn-143a46df: a blocker named there
    arms INV-ATTENTION-MISSING, and the deliberate silence is correct. The fix is to not open
    the investigation.

### §9 Out of scope, explicitly

* **ACCEPTED BLIND SPOT: a `gh` failure specific to ONE pull request.** A deleted repo, a
  typo'd URL or a permission loss on one PR leaves `Daemon.poll_default_branch` succeeding —
  so the project's fact stays fresh and the horizon keeps advancing — while that one order
  never polls. It stays excluded, and its open span keeps accruing, for as long as the
  project is healthy. Accepted by Neo on question 1998 and recorded here as accepted: it is
  the same error class the red-base hold already accepts, both halves rest on the same fact,
  and the per-pull-request bound that would close it is §8.6, rejected on cost. The bound
  this spec adds covers the failure mode that actually occurred — `gh` down for the whole
  project.
* **`holds.py` is untouched** — no `_episodes` synthesis, no new `HOLD_CAUSES` entry (§8.1).
* **No threshold changes.** `waiting_pr_merge` stays 60 minutes;
  `fleet_health.cooldown_minutes`, `max_per_day` and the fingerprint are untouched. The
  repeat-per-episode symptom is cured by not flagging the order at all.
* **No catalog key** of any kind, and `BASE_HEALTH_FRESH_SECONDS` stays 900 and stays
  `central_store`'s.
* **No schema change and no new per-tick write** — §3. `stuck_scan`'s signature is unchanged;
  only its docstring and the body of the `waiting_pr_merge` arm move.
* **No change to `Daemon._note_automerge_held`, its dedupe key, `automerge.decide`,
  `Daemon.poll_default_branch` or any `HELD_*` code.** The record already says the right
  thing; the defect was that the sweep did not read it, and then that it read it without a
  clock.
* **`ops.automerge_state`, `ops._automerge_line` and `jarvis wo show` are unchanged.** They
  were the surface that was already correct.
* **`invariants.status_label` gains nothing** — §5, with the read-budget reason.
* **No new notification and no attention flag** — §5, flagless.
* **The other `HELD_*` codes gain no exclusion.** Naming that here is what keeps the next
  extension from being assumed (the 2026-10-12 spec's closing rule).
