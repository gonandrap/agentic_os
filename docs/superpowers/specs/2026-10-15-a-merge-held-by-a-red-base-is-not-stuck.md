# A merge held by a red base is not stuck

Work order wo-9a88c96c. Fixes GitHub issue #1082. Fourth member of the exclude-and-discount
family after the usage window, the CI hold
(`docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md`) and
the in-flight turn (`docs/superpowers/specs/2026-10-14-a-worker-turn-in-flight-is-not-stuck.md`).

## The problem

`ops.stuck_scan` (src/jarvis/ops.py:9665) builds `stuck.assess`'s `excluded_cause` from
three holds and only three:

```python
excluded_cause=(PAUSE_USAGE_LIMIT
                if window or any(h.open for h in usage)
                else VALIDATION_CI_CAUSE if ci_held
                else stuck.LIVE_DEPENDENCY if live_edges else "")
```

`worker_session.PAUSE_USAGE_LIMIT`, `project_store.VALIDATION_CI_CAUSE` (scoped to
`validating`) and `stuck.LIVE_DEPENDENCY` (scoped to `pending`). There is no fourth.

So an order in `waiting_pr_merge` **whose auto-merge the OS is itself holding** is judged on
raw time in status against the default 1.0h `waiting_pr_merge` threshold
(`catalog.DEFAULT_FLEET_HEALTH_THRESHOLDS["waiting_pr_merge"] = 60`, src/jarvis/catalog.py:1079)
and flagged stuck. The hold is `automerge.HELD_BASE_RED` — `automerge.decide`'s seventh
condition, src/jarvis/automerge.py:358-370:

```python
    if base_red is not None:
        ...
        return _held(HELD_BASE_RED,
                     f"`{base_red.base}` itself is red — workflow ...
                     f"merges onto a broken default branch.{rider}")
```

built off the fact `Daemon._base_red` reads from `CentralStore.base_health`
(src/jarvis/daemon.py:7032-7058). Nothing is wrong with that order. The OS declined to merge
it, on purpose, because the project's default branch does not build; the hold clears with no
user decision in the loop, the moment `main` goes green.

Evidence that the sweep says otherwise anyway:

* **wo-05e0a06d** was swept at 4.9h in `waiting_pr_merge` while `main` was red.
* **inv-b50cbe1e** was the investigation that bought — a full model session — the answer
  "the OS is working as designed".
* `invariants.status_label` on the very same row already prints
  `auto_merge: held — `main` itself is red`. The sweep files an investigation to discover
  the sentence printed next to the order it is investigating.
* It is **once per red episode, per parked order**, for as long as `main` stays red: the
  per-subject cooldown (`fleet_health.cooldown_minutes`) bounds repeats for one subject, not
  the fleet's parked backlog.

Root cause, stated plainly: `excluded_cause`'s hold family is enumerated by hand and
`HELD_BASE_RED` was never added to it. This is not a threshold problem — raising the
`waiting_pr_merge` threshold would stop reporting genuine merge stalls, which is the one
thing the sweep exists for.

## The fix

Two changes, not one, because `waiting_pr_merge` is judged on **time in status** and not on
silence (`stuck.ACTIVITY_STATUSES` is `("running",)`). kn-0a222b38's pair rule: on such a
status an exclusion alone only POSTPONES the defect — the first sweep after the base
recovers sees the whole accumulated wait in one status span and buys exactly the
investigation the exclusion existed to prevent.

1. A named exclusion reaching the pure predicate (§1).
2. A central, per-project **red-episode list** whose spans are discounted, so the forgiveness
   survives recovery (§2). This half is Neo's ruling on question 1818 and is not re-opened
   here.

### §1 The exclusion

**A named cause in `src/jarvis/stuck.py`**, beside `LIVE_DEPENDENCY` and
`TURN_IN_FLIGHT_CAUSE` (src/jarvis/stuck.py:33-44):

```python
#: §1 of docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md.
BASE_RED_CAUSE = "the default branch is red"
```

with **its own sentence** in `EXCLUSION_REASON` (src/jarvis/stuck.py:49-59), because
`DEFAULT_EXCLUSION_REASON`'s "held by {cause}, which clears by itself" is true but says
nothing a reader can act on:

```python
    BASE_RED_CAUSE: ("the OS is holding this merge itself: the default branch's own build "
                     "is broken and nothing merges onto a red base — it clears when the "
                     "base goes green, with no decision from you, so nothing is judged "
                     "while that is true"),
```

**It reaches `stuck.assess` as `excluded_cause`.** Never a `continue` in `ops.stuck_scan`
(kn-143a46df, kn-e91ff3a5): a `continue` has no unit test over the pure predicate and
produces no `Verdict` row, so the held order would vanish from `jarvis stuck` and `/stuck`
instead of appearing there with its reason. `stuck.assess` is unchanged apart from the new
constant — the existing `elif excluded_cause:` branch already composes the sentence and
still populates `active_seconds` and `threshold_seconds`, so the row shows the arithmetic.

**Resolved in `ops.stuck_scan` from a per-project bool the CALLER hoists**, exactly as
`window` is hoisted today (new keyword-only parameter beside it):

```python
def stuck_scan(pstore, project_name, cfg, now, *, window=False,
               base_red=False, red_episodes=(), base_red_until=0.0,
               stuck_only=True):
```

`base_red` is true when **both** hold:

* the project's `CentralStore.base_health` fact is **red and FRESH** —
  `central_store.BASE_HEALTH_FRESH_SECONDS` (src/jarvis/central_store.py:22); and
* the project opted into auto-merge: `validation.enabled and validation.auto_merge`.

**Scoped to `status == "waiting_pr_merge"` ONLY** (kn-6e836793). A `needs_review` order on
the same red base is still a wait the USER owes — the OS is not holding it, the reviewer is
— and must stay judged. Nothing else is in scope either: a red base does not stop a worker
typing in `running` or a dispatch in `pending`.

Placement in the `excluded_cause` chain: after the `PAUSE_USAGE_LIMIT` branch (a fact about
the whole account outranks a fact about one project) and alongside the CI branch. Order
against `VALIDATION_CI_CAUSE` and `LIVE_DEPENDENCY` is arbitrary and must be documented as
such in the comment: the three are scoped to `validating`, `waiting_pr_merge` and `pending`
respectively and are mutually exclusive by status.

**Where the two callers get the bool.** Both already hold everything needed; neither adds an
I/O call per candidate.

* `Daemon._stuck_candidates` (src/jarvis/daemon.py:5498-5517) has `self.central` and
  `project.validation`. It reuses **`Daemon._base_red`** (src/jarvis/daemon.py:7032) rather
  than re-deriving freshness: `base_red = self._base_red(project) is not None` under the same
  `project.validation.enabled and project.validation.auto_merge` guard
  `Daemon.poll_pull_requests` puts on that read (src/jarvis/daemon.py:7185-7186). The
  freshness test is `_base_red`'s, **reused and not re-derived** — a second comparison against
  `BASE_HEALTH_FRESH_SECONDS` is a second definition of "the OS knows the base is red".
* `ops.stuck_report` (src/jarvis/ops.py:9770) has the `ProjectSpec` as `spec` and the
  existing reader **`ops._stored_base_health`** (src/jarvis/ops.py:5318), which already
  returns `{}` for unreadable. It applies the same two conditions over that dict:
  `spec.validation.enabled and spec.validation.auto_merge`, `fact.get("red")`, and
  `now - float(fact["checked_at"]) <= BASE_HEALTH_FRESH_SECONDS` with a non-numeric
  `checked_at` treated as stale. These three lines are the ONE place the fact is interpreted
  outside the daemon and go in a small helper next to `_stored_base_health`
  (`ops._base_is_red(spec, now)`), so the CLI/dashboard reader and the tick cannot disagree
  about what "red" means.

Read **once per project**, never once per candidate — `health_sweep_hold`'s rule, restated in
`poll_pull_requests`' comment at src/jarvis/daemon.py:7182-7184: the fact is the project's
and not the order's.

### §2 The discount

**A central, per-project RED-EPISODE LIST.** Neo's ruling on question 1818.

`Daemon._check_base_health` (src/jarvis/daemon.py ~6936-6995) already computes
`was_red = bool(prev.get("red"))` from the previous fact and already branches on both
transitions (src/jarvis/daemon.py:6984, 6994). The list is appended to **on a TRANSITION
only**, in those two existing branches — never once per poll:

* green to red: append `[start, null]`, an **open** pair.
* red to green: close the last open pair by writing its `end`.

**Storage.** New accessors beside `set_base_health` / `base_health`
(src/jarvis/central_store.py:2271-2293) and a new `os_state` key beside `base_health_key`
(src/jarvis/central_store.py:25-27), for that function's stated reason — the fact is
fleet-shaped and belongs beside `daemon_pid` in `os_state`:

```python
def base_red_episodes_key(project: str) -> str:
    """The `os_state` key holding one project's red-base episodes."""
    return f"base_red_episodes:{project}"
```

* `CentralStore.base_red_episodes(project) -> list[list[float | None]]` — tolerant exactly as
  `base_health` is: `[]` for absent, unparseable, or not-a-list-of-pairs. An unreadable list
  discounts nothing, which is the under-discount direction (see below).
* `CentralStore.set_base_red_episodes(project, episodes)` — dumb `json.dumps` writer.

**Two PURE functions, both module-level in `src/jarvis/central_store.py`**, taking the list
rather than a `CentralStore`:

```python
def advance_red_episodes(episodes, red, now, retention_seconds): ...
def base_red_hold_spans(episodes, since, now, open_end): ...
```

*Why there.* `project_store.validation_ci_hold_spans` (src/jarvis/project_store.py:253-285)
is the precedent and its docstring gives the rule: the span derivation lives in the module
that OWNS the rows, beside the cause constant it filters on, and takes rows rather than a
store — "one home, two carriers". Here the rows are the `os_state` JSON that
`central_store.py` owns and `base_health_key` already keys, so the pure functions belong
there and not in `ops`, `stuck` or `holds`. `stuck.py` is excluded by its own opening
paragraph (nothing there opens a store or knows a row shape) and `holds.py` by
`union_seconds`' ("pure arithmetic over pairs: no `Hold`, no cause, no clock").

`advance_red_episodes` is the writer's whole policy: open on a green-to-red transition, close
the open pair on red-to-green, drop pairs whose `end` is older than `now - retention_seconds`,
and return the list unchanged when the state did not transition. Idempotent, so a double call
in one tick cannot open two episodes. `Daemon._check_base_health` calls it once and passes the
result to `set_base_red_episodes` in the same two branches that already run on a transition.

`base_red_hold_spans` returns `[(start, end)]` clipped to `[since, now]`, `end` of an open
pair taken as `open_end`, and drops empty spans — `validation_ci_hold_spans`' `if end > start`
rule, so a pair entirely before the current status span contributes nothing.

**`open_end`, and why it is not simply `now`.** `_check_base_health` returns early and writes
nothing on `github.GitHubError` (src/jarvis/daemon.py:6958-6960), so a `gh` that stopped
answering leaves an episode open for ever. Running it to `now` would make the discount grow
without anybody confirming the base is still red — an over-discount, the one direction that
must never happen. So the caller hoists `open_end = min(now, checked_at + BASE_HEALTH_FRESH_SECONDS)`
from the **same fact read the §1 bool comes from** (no extra I/O), where `checked_at` absent
or non-numeric gives `open_end = since`, contributing nothing. A stale fact therefore excludes
nothing AND stops discounting at the freshness horizon: one coherent story, both halves
degrading the same way.

**Where it is applied in `ops.stuck_scan`.** The LIST is read once per project by the caller
and passed in (`red_episodes`, `base_red_until`); the CLIP is per candidate, because `since`
is per candidate — pure arithmetic over a bounded list, no store touched, exactly as
`validation_ci_hold_spans(events, since, now)` is called per candidate today. The spans join
the existing union:

```python
        discounted = holds.union_seconds(
            [(max(h.started, since), min(h.finish(now), now)) for h in usage]
            + ci_spans + base_red_spans)
```

**A UNION and never a sum** — `holds.union_seconds`' own docstring: a usage window can shut
while the base is red, and summing would tell a reader the same minutes twice under two
names.

`base_red_spans` is `[]` unless `wo["status"] == "waiting_pr_merge"`, for §1's reason: time a
`needs_review` order spent beside a red base is still time the user owed, and forgiving it
would hide a genuine review stall.

#### Why a single `red_since` field is not enough

A reviewer will propose adding `red_since` to the `base_health` fact and subtracting
`now - red_since`. It fails on the case the discount exists for.

`Daemon._base_red` returns `None` **the instant** `main` goes green
(src/jarvis/daemon.py:7047-7048), and `_check_base_health` overwrites the fact with
`red: False` in the same tick (src/jarvis/daemon.py:6972-6983). A discount read off the LIVE
fact therefore vanishes at recovery — while the order is still in `waiting_pr_merge`, because
the merge happens on a later `poll_pull_requests` tick and may be delayed again by a conflict
or a catch-up. The next sweep then judges the whole backlog of held hours against the 1.0h
threshold and files the investigation. That is kn-6e30d561's trap: **the discount must outlive
the exclusion by one status span.** Closed episodes in the list are what survive recovery;
a live field cannot.

#### The retention bound is a CATALOG KEY

Neo's one condition on 1818: the list's retention bound is resolvable fleet-wide and per
project under `fleet_health.*`, and is **never a module constant**.

* New field on `catalog.FleetHealthConfig` (src/jarvis/catalog.py:1152-1189):
  `red_episode_retention_hours: int = DEFAULT_FLEET_HEALTH_RED_EPISODE_RETENTION_HOURS`,
  default `168` (seven days) — long enough to cover a red base that spans a weekend plus the
  status span after it, short enough that the JSON stays a handful of pairs. Hours and not
  minutes because `thresholds`/`*_minutes` measure a sweep's patience in minutes and this
  measures how long the OS REMEMBERS; `10080` would read as a typo.
* Parsed in `_parse_fleet_health` (src/jarvis/catalog.py:2378-2441) in the existing
  `FleetHealthConfig(...)` construction, field-level inheritance like every sibling:
  `red_episode_retention_hours=int(raw.get("red_episode_retention_hours", base.red_episode_retention_hours))`.
* **No entry in `FLEET_HEALTH_LIST_KEYS` or `FLEET_HEALTH_MAP_KEYS`.** Those exist because
  comparing a tuple or a dict with an int raises `TypeError` in the reflective tail loop at
  src/jarvis/catalog.py:2435-2440 (`catalog.py:1134-1136` says so). A plain positive int is
  exactly what that loop validates, so `value < 1` already refuses `0` and negatives naming
  the key, and nothing else is needed.
* **Not in `FLEET_HEALTH_FLEET_ONLY_KEYS`** either: how long one project's red episodes are
  remembered is per-project by nature, unlike a fleet-wide daily cap or the one fleet-wide
  run record.
* **The WRITER is the bound's reader.** `advance_red_episodes` does the trimming, so the
  bound is resolved where it is called: `Daemon._check_base_health` reads
  `project.fleet_health.red_episode_retention_hours` from the `ProjectSpec` it already has.
  `base_red_hold_spans` takes no bound — it clips to `[since, now]` and nothing else, which
  is also what caps the exposure if the user SHRINKS the bound: stale pairs linger until the
  next transition, and only the part of a pair overlapping the current status span could ever
  count.

#### Degradation direction

Per kn-6e30d561 and Neo 1804, every failure of this mechanism must under-discount:

* An episode trimmed off the end of the retention window yields **no span** — not one running
  to `now`.
* An episode that began before this ships is **absent** from the list and likewise yields no
  span; the first episode recorded is the first green-to-red transition after the release.
* An unreadable or malformed list is `[]`.
* A stale fact caps an open pair at the freshness horizon.

Each of those makes the sweep judge MORE time, never less. The failure mode is "one extra
investigation of an order whose base was red before the list existed", which is exactly
today's behaviour. The opposite error — a span that kept growing off an unconfirmed fact —
would silence a genuinely stuck merge, which is unacceptable.

### §3 The gate: with auto_merge off, such an order stays judged

An assumption already on this record and it is part of the design: **both halves require
`validation.enabled and validation.auto_merge`** — the same gate `Daemon.poll_pull_requests`
puts on reading the fact (src/jarvis/daemon.py:7185-7186).

With auto-merge off the OS holds nothing. `automerge.decide` is never consulted, no
`HELD_BASE_RED` exists, and the user merges by hand — so the merge is the wait THEY owe. By
kn-6e30d561's "which order owes the action" test, such an order **stays judged** and a parked
pull request nobody merged is reported, red base or not. The red-episode list may still be
written for such a project (it is a project fact, and `_check_base_health` runs regardless),
but `stuck_scan` receives `red_episodes=()` and `base_red=False` for it, so nothing is
excluded and nothing is discounted.

### §4 Surface

**No new renderer, and that is a finding rather than a decision.** Confirmed in the code:

* `ops.stuck_report` already carries `"excluded": verdict.excluded` and
  `"reason": verdict.reason` on every row (src/jarvis/ops.py:9808), for every project with
  `fleet_health.enabled`.
* `cli.cmd_stuck` prints `row['reason']` for every row, stuck or not (src/jarvis/cli.py:3027),
  and `--json` dumps the row whole.
* `src/jarvis/ui/templates/stuck.html` has an `excluded` column rendering
  `{{ r.excluded or '—' }}` (line 61) in the lower "record" table.

So the `Verdict.excluded` row shows up on `jarvis stuck` and `/stuck` with the named cause
and its sentence the moment §1 lands, with **one** edit: the explanatory paragraph in
`stuck.html` (lines 42-47) enumerates the holds in prose — "the account's usage window, a
fleet pause, the reopen ramp, or a validation round waiting for GitHub…" — and must name this
one too, or the page lists a cause its own legend denies. `stuck.assess`'s docstring and
`stuck.py`'s `excluded_cause` paragraph get the same addition, for the same reason.

### §5 Test plan

`tests/test_health_sweep.py` and `tests/test_validation_ci_hold.py` are the precedents for a
sweep exclusion+discount pair; `tests/test_base_red.py` covers the base-health fact. Extend
those three — no new file.

In `tests/test_validation_ci_hold.py`'s shape (the pair's home) or `tests/test_health_sweep.py`:

1. **Held and excluded with the named cause.** `waiting_pr_merge` for 4.9h (wo-05e0a06d's
   number), fresh red fact, auto_merge on: `verdict.stuck is False`,
   `verdict.excluded == stuck.BASE_RED_CAUSE`, and `verdict.reason` is
   `EXCLUSION_REASON[BASE_RED_CAUSE]` rather than `DEFAULT_EXCLUSION_REASON`.
2. **`needs_review` on the same red base is still judged.** Same project, same fact, 4.9h:
   `stuck is True`, `excluded == ""`. The regression guard for kn-6e836793's scoping.
3. **auto_merge off is still judged.** Identical to case 1 with
   `validation.auto_merge = False`: `stuck is True`. Also with `validation.enabled = False`.
4. **The base going green leaves the closed episode discounted.** The kn-6e30d561 case and
   the reason the discount exists: 4.9h in `waiting_pr_merge`, of which 4.5h is a CLOSED
   episode, fact now green. `excluded == ""` (nothing is held any more) and `stuck is False`,
   because `active_seconds ≈ 0.4h` is inside the 1.0h threshold. Assert `active_seconds`, not
   just `stuck` — the number is the claim.
5. **A stale fact excludes nothing.** Red fact with
   `checked_at = now - BASE_HEALTH_FRESH_SECONDS - 1`: `stuck is True`, `excluded == ""`, and
   the open episode contributes at most up to the freshness horizon — assert `discounted` is
   capped there rather than running to `now`.
6. **Union, not sum.** A usage hold fully inside the red episode: `discounted` equals the
   union and not the total of both. `union_seconds`' own rule, asserted through `stuck_scan`.
7. **No `continue`.** With `stuck_only=False` the held order is PRESENT in `stuck_scan`'s
   rows and in `ops.stuck_report`, carrying `excluded` — the structural guard kn-143a46df
   asks for.
8. **Both callers agree.** `Daemon._stuck_candidates` and `ops.stuck_report` over the same
   store, fact and clock produce the same verdict for cases 1 and 3 — the §7 "one derivation"
   invariant, and the test that would have caught a bool hoisted in one caller only.

In `tests/test_base_red.py`:

9. **Appends on a TRANSITION only.** Three red polls in a row produce ONE open pair; the
   green poll closes it; a second green poll changes nothing.
10. **A crash while red does not lose the episode.** `was_red` is read from the stored fact,
    so a fresh `Daemon` seeing green after a restart still closes the open pair.
11. **`advance_red_episodes` / `base_red_hold_spans` as pure units**: clipping to
    `[since, now]`, an open pair taking `open_end`, a pair wholly before `since` yielding
    nothing, an unparseable list yielding `[]`.
12. **An episode outside the retention window under-discounts.** With the bound at 1 hour, an
    episode closed 3 hours ago is trimmed on the next transition and yields NO span — assert
    the sweep then flags the order, i.e. the degradation is in the under-discount direction.
    Paired with: a list with no episode at all (the pre-ship case) discounts nothing.

In `tests/test_catalog.py` (the bound is a catalog key):

13. **Resolvable fleet-wide and overridden per project.** A project naming
    `red_episode_retention_hours` keeps the fleet answer for every other `fleet_health` field;
    a project naming nothing inherits the fleet value; `0` and `-1` are refused naming the
    key by the existing tail loop; it is NOT refused as a fleet-only key.

### §6 Rejected alternatives

1. **Synthesise the hold inside `holds._episodes`** — the cheap fix, and the one a reviewer
   will propose, since `holds` already models held spans. REJECTED on kn-0a222b38's
   blast-radius clause: `holds` feeds `jarvis inspect`, `Daemon.check_burning_turns`, the
   alarms and the feature rollups, and all of them would silently start subtracting red-base
   time from wall clock. A turn that ran for two hours did run for two hours. Worse, `holds`
   clips to turns, so the red-base span would be distorted by a rule that has nothing to do
   with it. The sweep is the only consumer that should forgive this time, so the span is
   derived for the sweep.
2. **Raise the `waiting_pr_merge` threshold.** Treats the symptom, and buys a second bug: a
   pull request nobody merged for six hours with a GREEN base is precisely what the sweep
   must report.
3. **A single `red_since` on the `base_health` fact.** §2 above — it vanishes at recovery and
   the first sweep after green judges the whole backlog.
4. **Derive the spans from the `automerge_held` / `HELD_BASE_RED` timeline event.** Attractive
   because the hold is already on the order's own timeline. REJECTED on two facts:
   `Daemon._note_automerge_held` (src/jarvis/daemon.py:9049) dedupes per
   `(head_sha, code, reason, round)` and **nothing marks the hold's END** — there is no event
   saying "the base went green", so every span would be open-ended, which is the over-discount
   direction §2 forbids. And `ops.record_base_health`'s `pr_base_health` transition events
   (src/jarvis/ops.py:7815), which DO record both directions, are written only from
   `Daemon.heal_inherited_failure` (src/jarvis/daemon.py:9249) — reached only when the PULL
   REQUEST's own checks are red. An order held with a GREEN pull request onto a red base, the
   common shape of this defect, has no span source there at all.
5. **A `continue` in `ops.stuck_scan`.** kn-143a46df, kn-e91ff3a5: no unit test over the pure
   predicate, no `Verdict` row, and the held order disappears from `jarvis stuck` instead of
   explaining itself there.

### §7 Not covered

* **The attribution of the red base** (`Daemon._attribute_base_red`) and the inherited-failure
  heal are untouched. This spec changes who gets INVESTIGATED, not who broke `main`.
* **`invariants.status_label` and `ops.automerge_state` are unchanged.** They already say the
  right thing; the defect was that the sweep did not read them.
* **No new notification.** A held merge is ordinary (`_note_automerge_held`'s
  "DELIBERATELY NOT AN ATTENTION ITEM"), and the recovery notice already exists
  (src/jarvis/daemon.py:6986-6992).
* **No change to `fleet_health.cooldown_minutes`, `max_per_day` or the fingerprint.** The
  repeat-per-episode symptom is cured by not flagging the order at all.
