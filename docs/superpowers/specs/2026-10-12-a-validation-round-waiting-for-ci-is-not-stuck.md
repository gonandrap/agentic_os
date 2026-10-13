# A validation round waiting for CI is not stuck

GitHub issue 1020. Neo question 1625 decided the mechanism; this spec records the
reconciliation and the exact shape.

## The problem

`ops.stuck_scan` (`src/jarvis/ops.py:9048`) excludes exactly one hold:

```python
excluded_cause=(PAUSE_USAGE_LIMIT
                if window or any(h.open for h in usage) else "")
```

So a work order in `validating` whose round the OS is deliberately holding for GitHub
checks is judged on the plain `validating` clock —
`catalog.DEFAULT_FLEET_HEALTH_THRESHOLDS["validating"] = 15` minutes
(`src/jarvis/catalog.py:1049`) — while `.github/workflows/ci.yml`'s `unit` job is the whole
suite and takes 21–22 minutes (measured on wo-16a488ee: 1273s and 1315s). The
hold itself is written by `Daemon._validation_ci_held` (`src/jarvis/daemon.py:3157`) with
cause `project_store.VALIDATION_CI_CAUSE = "ci_pending"` and is invisible to the sweep.

Observed wo-eeff70d3, 2026-10-12: PR #1016, `unit (3.13)` IN_PROGRESS at 28 minutes, the
sweep bought an investigation, and its blocker column read `nothing the record can name`
while `invariants.status_label` on the same row already printed
`invariants.CI_HOLD_NOTE` — "waiting for GitHub to finish running the checks"
(`src/jarvis/invariants.py:1632`). One surface knew and the other spent a session. It
fires on essentially every pull request the fleet validates, because the threshold is 15
minutes and the wait is always over 20.

### Root cause, and it is two facts, not one

1. **No exclusion.** The hold is not in the sweep's exclusion set.
2. **`validating` is not in `stuck.ACTIVITY_STATUSES`** (`src/jarvis/stuck.py:28`), so it
   is judged on **time in status**. An exclusion alone only postpones the defect: the
   moment the hold lifts, `seconds_in_status` is already past 15 minutes, so the first
   sweep after CI finishes buys exactly the investigation it was supposed to prevent —
   while the panel is legitimately mid-judgement. The held seconds must be **discounted**
   as well as excluded.

### Reconciliation with the standing ruling

kn-6e836793 (2026-09-27) says the only hold exclusion is an order paused on the Claude
usage/session limit, and `stuck.assess`'s docstring repeats it ("§2: … are the only ones").
This spec **extends** that ruling to `ci_pending` on the stated grounds that it is the same
shape as a usage window, not a new category:

- the OS is **deliberately not acting** — `_validation_ci_held` costs no outage attempt, no
  seat, no round number, no attention flag;
- the hold **clears without anybody being asked** — GitHub reports a conclusion, the next
  `validation_tick` reopens the same round. There is **no user decision in the loop**, so
  an investigation can only report that the OS is working as designed.

Any hold failing either test stays judged. This is an extension with a reason on the
record, not a silent widening of the exclusion set.

## The fix

Exclude while the hold is live **and** discount the held seconds — exactly the pair
`stuck_scan` already applies to `PAUSE_USAGE_LIMIT` at that one call site. No new
`fleet_health` catalog key.

### 1. Liveness and cause, at the one call site that holds a store

In `ops.stuck_scan`, for a row whose `wo["status"] == "validating"` only:

```python
latest = pstore.latest_validation_round(wo_id=wo["id"])
until, cause = project_store.validation_hold(
    pstore.events_of_kind(wo["id"], "validation_failed"), int(latest["round"]))
ci_held = bool(latest) and cause == VALIDATION_CI_CAUSE and until > now
```

This is the derivation `invariants.validation_hold_note`
(`src/jarvis/invariants.py:1643`) already uses, so the sweep's exclusion and the label the
same row prints cannot drift. `stuck_scan` already calls `invariants.status_label`, which
already performs both reads, so the cost is two indexed reads it is morally already paying.

**Gated on `validating`, deliberately.** A round runs in parallel with `needs_review`
(issue 212). A `needs_review` order is waiting on the **user**; excluding or discounting
there would hide a user-owed wait, which is the one thing the sweep exists to surface.

### 2. Self-clearing without a GitHub call

`_validation_ci_held` rewrites the `validation_failed` event every
`daemon.CI_HOLD_RECHECK_SECONDS` (60s, `src/jarvis/daemon.py:481`) for as long as checks
are pending, so `reopens_at` is continuously refreshed and liveness needs no network. When
every check has **reported a conclusion**, `evidence.ci_pending` returns empty, no further
event is written, and the hold expires within 60s — after which the order is judged
normally. That is how the issue's requirement is met: *a round parked with every check
concluded is still judged.* `daemon.CI_HOLD_DEADLINE_SECONDS` (45 min,
`src/jarvis/daemon.py:491`) bounds it in the other direction regardless.

### 3. The discount: one new pure function in `project_store.py`

Beside `validation_hold`, same rows-not-store carrier rule its docstring states (one home,
two carriers — a feature order's events come through `ops.feature_events_of_kind`):

```python
def validation_ci_hold_spans(events: Iterable[Any], since: float,
                             now: float) -> list[tuple[float, float]]
```

Each `validation_failed` event whose payload `cause == VALIDATION_CI_CAUSE` covers
`[event.ts, min(reopens_at, now)]`. Clip every span to `[since, now]`, drop the empty
ones, return them oldest first. **All rounds, no round filter**: clipping to `since` — the
current status span — makes round filtering unnecessary and still correct, because a round
only holds for CI while the order is in `validating`.

**Name**, and it is the third candidate not the first: `ci_hold_seconds` lies about the
return type, `validation_ci_held_seconds` reads as a boolean-ish past participle, and
neither can be unioned with anything. `validation_ci_hold_spans` keeps the `validation_`
prefix the three neighbours share (`validation_hold`, `validation_hold_until`,
`validation_standing`), names the cause, and says it returns intervals.

**Spans and not a total — this corrects the brief.** A scalar cannot be unioned with the
usage intervals (§4), and producing one here would force the double-count arithmetic into
the caller. The measure is one step later.

Coverage is the HELD interval and not CI's runtime, and that is the right measure — this
corrects an earlier draft's "conservative by up to 60s at the tail". `validation_tick`
goes again the moment `reopens_at` lapses (`src/jarvis/daemon.py:2575-2577`; the hold in
`tests/test_validation_ci_hold.py:131` lifts only by EXPIRING, which is why that test
shortens the recheck delay rather than rewriting the event), so each event's
`[ts, reopens_at]` is exactly the span the OS refused to judge in. Two residues, both
harmless: between one hold lapsing and the next recheck event being written the record
says nothing, which under-discounts by about one 5s tick per recheck; and the LAST span
runs to its `reopens_at` even if the checks concluded a few seconds after it was written,
which over-discounts by up to 60s of time nobody was judging in anyway. Inventing a span
past the last thing the record says would be the sweep guessing.

### 4. No double counting

`stuck_scan` already computes the usage figure with
`holds.by_cause(usage, since, now, now)`, and a usage window can shut while CI runs — the
two intervals overlap. The number handed to `stuck.assess` as `discounted_seconds` is the
**measure of the union** of the usage hold intervals and the CI spans inside
`[since, now]`, never their sum. `holds.py`'s own rule is the precedent: "the overlap is
counted ONCE … a reader can never be told the same four hours twice under two names"
(module docstring; `_merge`).

The union lives in `holds.py`, which owns this arithmetic:

```python
def union_seconds(intervals: Sequence[tuple[float, float]]) -> float
```

Pure, sorted-sweep, no `Hold` and no cause. `stuck_scan` feeds it the usage holds clipped
to `[since, now]` (`Hold.overlap`'s clipping, as intervals) plus the CI spans.

**With no CI hold the usage number is unchanged** — `union_seconds` over the usage
intervals alone equals `by_cause`'s total, because `holds.held` has already merged them.
That identity is a test (§7).

### 5. The exclusion reaches `stuck.assess` as data

`excluded_cause=VALIDATION_CI_CAUSE` when `ci_held`, alongside the existing
`PAUSE_USAGE_LIMIT` branch; `stuck.assess` stays pure and returns
`Verdict.excluded == "ci_pending"`, `stuck=False`.

**No `continue` in `stuck_scan`.** kn-143a46df and kn-e91ff3a5: a `continue` is
indistinguishable from an order comfortably inside its threshold, and the row vanishes
from `jarvis stuck` and `/stuck`. An excluded `Verdict` is a **visible row** naming the
cause, which is the whole difference between "the OS decided not to judge this and says so"
and "the OS forgot about it".

Usage wins a tie (it is checked first): nothing in the OS outranks the account, which is
`holds._RANK`'s rule already.

### 6. Surfaces — one prose line changes, no new label

- `cli.cmd_stuck` (`src/jarvis/cli.py:2931`) prints `row["reason"]`, which `stuck.assess`
  renders as `held by {excluded_cause}, which clears by itself — nothing is judged while it
  does`. Today that reads `held by usage_limit`; with this it reads `held by ci_pending`.
  Parity, no change needed.
- `src/jarvis/ui/templates/stuck.html:58` prints `{{ r.excluded or '—' }}` as a raw token.
  No change needed.
- **`src/jarvis/ui/templates/stuck.html:42–44` MUST change.** Its prose enumerates the
  exclusions — "the account's usage window, a fleet pause or the reopen ramp" — and becomes
  false the moment a fourth exists. Add the CI wait.
- **`stuck.assess`'s docstring MUST change** (`src/jarvis/stuck.py:76–80`): it states the
  usage window, the fleet pause and the ramp are "the only ones". Record the extension and
  cite kn-6e836793.
- `holds.HOLD_CAUSES` is **not** extended. Nothing renders `Verdict.excluded` through it;
  its keys also gate `_episodes`' `turn_paused` reason parsing, and every entry is a
  Claude-account or OS-internal fact, not a GitHub one. A fifth cause there would change
  `jarvis inspect` and the burn alarms, which is §8's rejected alternative.

### 7. Tests the implementer writes

`tests/test_health_sweep.py` (it already exercises `stuck.assess` at lines 1310–1333 and
owns the sweep):

1. A `validating` order with a **live** `ci_pending` hold, 20 minutes in status: the row
   comes back `excluded == "ci_pending"`, `stuck is False`, and is **present** in the
   `stuck_only=False` output — the anti-`continue` assertion.
2. The same order with no `ci_pending` hold at 20 minutes in status: still `stuck is True`.
   The exclusion must not generalise to every `validating` order.
3. Usage and CI holds overlapping: `discounted_seconds` equals the union, strictly less
   than the sum.
4. No CI hold at all: `discounted_seconds` equals today's `holds.by_cause` usage figure
   exactly.
5. `validation_ci_hold_spans` directly — spans clipped to `[since, now]`, several rounds,
   a hold that began before `since`.

`tests/test_validation_ci_hold.py` (it owns the hold's lifecycle; `fleet`/`fake_gh`
fixtures and the hold-lifting case at line 130):

6. After the hold expires with every check concluded, the order **is** judged, and the
   discount keeps it inside the 15-minute threshold — the §1(2) half of the root cause,
   and the test that fails if only the exclusion is implemented.

### 8. Rejected alternatives

- **Synthesise a `ci_pending` hold inside `holds._episodes`** and let the existing
  `holds.held` / `by_cause` call pick it up, as the module already does for
  `VALIDATION_HELD_CAUSE` and `VALIDATION_AUTH_CAUSE` (`src/jarvis/holds.py:300–310`).
  This is the fix a reviewer will propose, and it loses on blast radius: `holds.held` is
  read by `jarvis inspect`, `Daemon.check_burning_turns`, the alarm thresholds and the
  feature rollups, so 22 minutes that those reports today call wall clock would become
  subtracted "held" time fleet-wide, with a new cause in `HOLD_CAUSES` and in the
  published `Hold.as_dict`. The two existing syntheses attribute their span to a
  **transport** cause that genuinely is the same fact as a worker pause; there is no
  transport pause for GitHub. Also `holds.held` clips holds to intervals where no worker
  turn was alive, so a parallel round's hold would be silently zeroed and the sweep's
  number would depend on turn rows.
- **Raise the `validating` threshold to 30+ minutes.** Fixes the symptom for one CI
  duration and breaks the one thing the threshold is for: a panel genuinely wedged for 25
  minutes stops being seen. The wait is not slow, it is **held**, and those are different
  facts.
- **A `fleet_health.ci_hold_*` catalog key.** A setting asks the user to know GitHub's
  runtime. The hold is already in the record with a `reopens_at`; reading it is free.
- **Name the blocker in `invariants.true_blockers`** so the investigation prompt at least
  reads sensibly. kn-143a46df: a blocker named there arms INV-ATTENTION-MISSING, and the
  deliberate silence is correct. The fix is to not open the investigation.

### 9. Out of scope, explicitly

- The `validating` threshold default stays 15 minutes.
- `invariants.true_blockers`' deliberate silence is untouched.
- No new catalog key.
- No change to `Daemon._validation_ci_held`, `CI_HOLD_RECHECK_SECONDS` or
  `CI_HOLD_DEADLINE_SECONDS`.
- `VALIDATION_HELD_CAUSE` and `VALIDATION_AUTH_CAUSE` are **not** added to the exclusion
  set here. The usage one already is, by its own path (`holds` synthesises it as
  `PAUSE_USAGE_LIMIT`). The auth hold fails the second test of §"Reconciliation": it clears
  when a **human** runs `/login`, so an order held there is exactly the case the sweep
  should surface. Naming that now is what keeps the next extension from being assumed.
