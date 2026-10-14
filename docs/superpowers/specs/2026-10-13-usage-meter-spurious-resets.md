# The usage meter counts sub-second jitter as a window reset

Work order wo-4e2b94be, issue 1026. Amends
docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md (that spec's §4
rules 2, 4 and 6, its §9 sentence, its §8 alarm gate). Deterministic, read-only, no model
call. No schema change: the stored samples are correct, the arithmetic over them is not.

## The problem

`usage_meter._boundary` (src/jarvis/usage_meter.py:318-327) calls a boundary a reset
whenever `a.resets_at(key) != b.resets_at(key)` — raw float inequality, per §4 rule 2
("that is the authoritative signal"). The endpoint's `resets_at` carries **sub-second
jitter**: consecutive production samples read

```
1791882000.49662  1791882000.479002  1791882000.321001  1791881999.789849
```

— the same 17:00:00 wall-clock instant, differing by milliseconds. Under `!=`, every
consecutive pair is a reset.

Consequence in `segments` (usage_meter.py:329-386): the loop breaks a segment at every
sample, so every segment is one sample long (`since == until` in each `_window_dict`
row, usage_meter.py:813), and every segment after the first starts at `start_pct = 0.0`
by §4 rule 4. Each sample's FULL utilisation is therefore summed as fresh usage.

Measured, production week 1791864000-1792468800:

| | reported | arithmetically possible |
|---|---|---|
| `reset_count` | 287 | at most 34 (a 7-day span holds ⌊604800/18000⌋+1 5h windows) |
| `delta_points` | 19876 | 87 (true monotonic rise 0.0 -> 87.0 pct) |
| `anomalies` | 0 | — nothing flagged it |

At `cfg.meter_dollars_per_point` 0.82 the /cost page printed ~$16227 implied and, since
measured Jarvis spend and measured outside sessions are real and small, attributed ~99% of
it to the §7 residual — i.e. told the user that usage on other machines dominated their
bill. `anomalies` stayed 0 because §4 rule 5 only counts a NEGATIVE delta, and 0.0 -> pct
is never negative.

**Root cause, one sentence:** §4 rule 2 treats an equality test on a float that the
endpoint only guarantees to the window, not to the microsecond, as a state-change
detector — and `segments` has no post-condition that would have caught the result being
impossible.

Two contributing defects, both load-bearing and both in scope:

* **No bound.** Nothing in `segments` compares `reset_count` against the number of windows
  the span can hold, nor a segment's delta against the meter's own 0-100 range. A 287x
  error left the module silently.
* **Confident wording over an underivable number.** `sentence`
  (usage_meter.py:763-802) prints `~$16227` and, at `coverage.share` 0.03, appends "the
  meter covers only 3% of this span, so the rise is a lower bound" — which is false: with
  97% of the span unmeasured the rise is not a bound in either direction. `reconciliation`
  (usage_meter.py:830-910) computes `implied`/`residual` from `delta_points` whenever
  `covered` (one in-span sample) is true, and `alerts` (usage_meter.py:913-934) threshold
  against those same numbers, so `daemon.check_meter_residual` (daemon.py:5828-5885, reads
  `meter["alerts"]`) will inbox a residual alarm computed from nonsense.

## The fix

Three parts: the detector gets a tolerance, `segments` gets post-conditions, and every
surface stops asserting a dollar figure it cannot derive. §4 rules 2, 4 and 6 are amended;
rules 1, 3 and 5 stand unchanged.

### 1. The detector — an ADVANCE past a tolerance, in `_boundary`

New module constants beside `DROP_TOLERANCE` (usage_meter.py:43-45):

```python
#: `resets_at` is the same INSTANT to the window, not to the microsecond: production
#: samples of one 17:00:00 reset differ by ~0.7s. A reset is an ADVANCE past this; a
#: genuine advance is never smaller than the window itself (a new window's reset is its
#: own start + the window length, and its start is at or after the old reset). One
#: SAMPLE_SECONDS: nothing finer than one sampling period is resolvable from this series.
RESET_TOLERANCE_SECONDS = SAMPLE_SECONDS   # 60.0

#: Each window's length, per `FIRST_CLASS` key — the bound in (2) is per key and a
#: hard-coded 5h would let a 7-day span claim 34 weekly resets.
WINDOW_SECONDS = {"five_hour": 5 * 3600.0, "seven_day": 7 * 86_400.0}
```

`_boundary(a, b, key)` becomes, with both `resets_at` present:

1. `after - before > RESET_TOLERANCE_SECONDS` -> `"reset"`. The condition is **advance**,
   not difference: the endpoint moves the reset forward by at least one window length
   (>= 18000s for `five_hour`), so the test has three orders of magnitude of headroom
   either side of the jitter.
2. `abs(after - before) <= RESET_TOLERANCE_SECONDS` -> jitter. Not a boundary, in either
   direction. This is the whole 287-to-0 change.
3. `before - after > RESET_TOLERANCE_SECONDS` (moved BACKWARDS past the tolerance) -> not
   a reset, and `cause` stays "": a window's reset instant never retreats, so this is the
   endpoint contradicting itself. It is counted in `anomalies` (a new `anomalies` cause,
   see 2) so it is visible, and the existing pct-drop rule still catches the real reset if
   one happened. Breaking a segment here would re-import the bug in a rarer form:
   `start_pct = 0.0` on a signal we have just called untrustworthy.

The pct-drop rule (`pct_b < pct_a - DROP_TOLERANCE` with `resets_at` unchanged) is
unchanged and remains the backstop for a reset the endpoint reports late — with the
tolerance in place, "unchanged" now means "within tolerance", which is what §4 rule 2
meant to say.

`_runs` (usage_meter.py:399-420) calls `_boundary` and is fixed by the same change: today
every sample pair breaks a run, so no run reaches `meter_calibration_min_minutes`, no span
is clean, and `dollars_per_point` can only ever return `source: "seed"` — which is why the
0.82 seed was the multiplier in the production figure above. Nothing in `_runs` needs
editing.

### 2. The bound — post-conditions inside `segments`, before it returns

Where: at the tail of `segments` (usage_meter.py:384-386), after `delta_points` and
`reset_count` are computed and before the return. There and not in `reconciliation`,
because `segments` is the only function that knows the span and the key, and because
`_runs`, `reconciliation` and any later caller must all inherit the guarantee.

`SegmentSum` (usage_meter.py:310-316) gains two fields:

```python
reliable: bool = True
unreliable: list[str] = field(default_factory=list)   #: machine keys, never prose
```

Two checks, **clamp AND mark** — both, for different readers:

1. `max_resets = int((until - since) // WINDOW_SECONDS[key]) + 1`. If
   `reset_count > max_resets`: append `"reset_count_above_span"`, set `reliable = False`.
   `reset_count` is clamped to `max_resets` in the payload. Segments are NOT re-merged —
   the list stays as computed, because it is the evidence for the defect and a merged list
   would hide it.
2. Any `segment.delta > 100.0` (the meter reports whole percent, 0-100, so no single
   window can rise more than 100 points): that segment's delta clamps to 100.0, append
   `"segment_delta_above_window"`, set `reliable = False`. `delta_points` is summed AFTER
   the clamp, so the sum can never exceed `max_resets * 100`.

Clamping alone would print a plausible wrong number; marking alone would let a 19876 reach
a template that formats it with `'%.1f'`. A key with no entry in `WINDOW_SECONDS` (a future
window promoted to first-class) skips check 1 and appends `"window_length_unknown"` rather
than guessing a length.

Coverage is a third reason, decided in `reconciliation` and not here (it is about the span
and the sample series, not about one key's arithmetic):

```python
MIN_RELIABLE_COVERAGE_SHARE = 0.5   #: more of the span unmeasured than measured
```

`reconciliation` marks the derivation unreliable when `coverage.share <
MIN_RELIABLE_COVERAGE_SHARE` (`"low_coverage"`) or when
`coverage.longest_gap_seconds >= WINDOW_SECONDS["five_hour"]` (`"gap_longer_than_window"`
— a whole 5h window could have opened and closed inside the hole, so neither the rise nor
the reset count is bounded). The production case trips both.

### 3. The honesty — one gate, reused

`reconciliation` already has the "nothing can be derived" path: `covered` false gives
`implied_usd`, `residual_usd` and all three shares as `None` and the sentence
`NO_SAMPLES`. Generalise that gate rather than adding a second one:

```python
covered   = five.coverage.samples > 0          # unchanged meaning; feeds the coverage line
derivable = covered and five.reliable and seven.reliable and not span_unreliable
```

and every `if covered` that guards money (usage_meter.py:865-866, 873, 880) becomes
`if derivable`. Consequence, which is the point: `implied_usd`, `residual_usd`,
`residual_share`, `outside_share`, `jarvis_share` are `None` and no surface can print a
dollar figure or a percentage derived from the meter.

What stays a number: `workers_usd`, `jarvis_calls_usd`, `jarvis_usd`, `outside_usd` and the
`outside.sessions` rows. Those are measured directly from `wo_turns`, `agent_calls` and
transcripts — they do not depend on the meter arithmetic, and blanking them would lose true
information.

`meter` subtree gains `"reliable": derivable` and `"unreliable": [keys…]` at the top level,
plus `"reliable"`/`"unreliable"` per window in `_window_dict` and `"max_resets"` so a
reader can see the bound that was tripped. `_coverage_dict` (usage_meter.py:820-827) gains
`"reliable": coverage.share >= MIN_RELIABLE_COVERAGE_SHARE` and
`"min_reliable_share": MIN_RELIABLE_COVERAGE_SHARE` — the page's `{{ '%.0f'|format(100 *
meter.coverage.share) }}% covered` line (src/jarvis/ui/templates/_meter.html:31) then has
something to colour.

**`sentence` (usage_meter.py:763-802), the wording contract.** New constant beside
`NO_SAMPLES`:

```python
UNRELIABLE = ("the usage meter does not reconcile over this span and no dollar figure is "
              "derived from it")
```

When `derivable` is false, the sentence is `UNRELIABLE`, then the reason in plain words
(one clause per key: "more of the span is unmeasured than measured (3% covered)"; "a
4h58m gap is longer than the 5h window, so a whole window may have passed unseen"; "the
rise implies 287 window resets and the span can hold at most 34 — this is a defect in
Jarvis's own arithmetic, filed as such"), then the measured halves as measured: "Jarvis
spent $X; sessions this machine ran that Jarvis did not dispatch spent $Y."

Three things it must NOT do, each the exact failure above:

* **No residual, and no `RESIDUAL_LABEL`.** Attributing a gap to "usage this machine cannot
  see" requires the meter side of the subtraction to be sound. When it is not, the residual
  is a statement about Jarvis's arithmetic wearing the clothes of a statement about the
  user's other devices.
* **Never "lower bound".** Delete that branch's claim: the `coverage["share"] < 0.9` clause
  keeps the coverage FACT ("the meter covers only N% of this span") and loses "so the rise
  is a lower bound", which is only true when the missing samples can only have hidden
  usage — not when they can also have hidden a reset. Between
  `MIN_RELIABLE_COVERAGE_SHARE` and 0.9 the honest word is "incomplete".
* **No `$` from the meter.** No `implied`, no shares, no `dollars_per_point` applied to a
  clamped `delta_points`.

The `reset_count` clause ("so the rise is summed per segment") stays, and now says
something true.

**Callers, named, and what each needs.**

* `src/jarvis/cli.py:3127` and `:3158` print `meter['sentence']` verbatim and nothing else
  from the subtree. **No CLI change** — this is exactly why §9 put the wording in the
  payload.
* `src/jarvis/ui/app.py:1744-1756` passes the whole subtree to the template. **No change.**
* `src/jarvis/ui/templates/_meter.html` — line 10 renders the sentence (fixed by the
  above); lines 36-41 render `five_hour.delta_points`, `seven_day.delta_points` and
  `dollars_per_point.value` as bare numbers. Add one guarded line: when `meter.reliable` is
  false, the delta figures are rendered with the word "unreliable" next to them and the
  `$/point` figure is omitted. No arithmetic in the template, per its own header note.
* `src/jarvis/ops.py:13083 cost_meter` / `meter_samples` — pass-throughs. **No change.**

**`alerts` (usage_meter.py:913-934) with an unreliable reconciliation.** `big` already
requires `implied is not None`, so part 3 makes both flags `False` by construction once
`implied_usd` is `None` — but that must be explicit, not incidental:

```python
unreliable = bool(spend.get("unreliable"))     # or the subtree's list, threaded in
# both conditions gain `and not unreliable`
return {..., "unreliable": unreliable, "suppressed": unreliable and big_before}
```

`daemon.check_meter_residual` (daemon.py:5828-5885) reads only
`meter["alerts"]["outside"]` and `["residual"]`, so it raises nothing: correct, because both
alarm kinds make claims (`meter_alarm_reason`, daemon.py:324-346, writes "$N of the ~$M
implied by the 5h meter … is unexplained") that are exactly the false claims above. It gains
one `log.warning` naming `meter["unreliable"]` so a suppressed pass is visible in the log.

**No new alarm kind, and no new invariant.** Rejected: an inbox item for "the meter did not
reconcile". `invariants.check_usage_meter_stale` (src/jarvis/invariants.py:4842-4878)
already covers the case the user can act on — the endpoint is unreadable — and raises
`INV-USAGE-METER-STALE` from the rows themselves; low coverage over a historical span is
the same fact after the event, and a second channel saying it would double-notify. A bound
trip is a DEFECT IN THIS MODULE, not a condition in the fleet: tests catch it (part 4), the
payload says so, and the user has nothing to do about it. That invariant's own wording
carries the same "lower bound of unknown size" phrasing and is left alone: there it is
true, because it is about having no second number at all.

### Out of scope, deliberately

**`DEFAULT_COST_METER_DOLLARS_PER_POINT` = 0.82 (src/jarvis/catalog.py:1238) is not
touched.** It is the prime suspect for a $16227 figure and it is the wrong thing to change:
with `_boundary` broken, `_runs` returns no clean span, `dollars_per_point` can only answer
`source: "seed"`, and 0.82 has been multiplying a `delta_points` inflated 228x. Any
re-estimate made now would be fitted to the bug. Once part 1 lands, `_runs` produces real
spans and `dollars_per_point` reports `measured` with a band — and only then is there
evidence about whether 0.82 is right. A follow-up order re-measures it from a week of
post-fix samples.

Also out: the `resets_at` jitter itself (an endpoint property, nothing to fix here); the
§4 rule-3 head-coverage rule; the schema; the sampler; `fleetcost`'s per-order
distribution; backfilling or recomputing historical payloads (every surface derives from
stored samples on read, so a corrected read fixes history).

### Rejected alternatives

* **Round `resets_at` to the second on parse (`_epoch`, usage_meter.py:101-112).** Cheapest
  patch and it would silence this production case. Rejected: it stores a derived value and
  loses the raw reading, it still breaks on jitter that straddles a second boundary
  (…999.78 vs …000.49 round to different seconds — two of the four quoted samples do
  exactly this), and it leaves `segments` with no post-condition, so the next detector bug
  is silent again.
* **Compare on the floor of `resets_at / window_length`.** Same family, and wrong whenever
  a window is re-anchored after idle, where the new reset is not aligned to any grid.
* **Drop `resets_at` as the signal and detect resets from the pct drop alone.** Rejected
  for §4 rule 2's original reason: a window that resets while utilisation is near 0 shows
  no drop, so the reset is missed and the next window's rise is swallowed. The endpoint's
  own signal is better — it just needs a tolerance.
* **Clamp `delta_points` and print it anyway.** That is the current defect with a smaller
  number: 87 points of real rise and a clamp at 3400 both print as fact.
* **Make the tolerance a `cost.*` setting.** It is a property of the endpoint's payload,
  not a preference; a knob here is a knob whose only correct value is the shipped one.
  `DROP_TOLERANCE` and `MAX_SPAN_GAP_SECONDS` are module constants for the same reason.

### Tests

Files that exist today: `tests/test_usage_meter.py` (the sampler, the segment sum —
`test_a_span_with_no_reset_is_end_minus_start`,
`test_a_reset_inside_the_span_sums_two_segments`,
`test_a_drop_with_resets_at_unchanged_is_a_boundary_and_an_anomaly`,
`test_two_resets_inside_one_span_give_three_segments`,
`test_a_seven_day_reset_runs_through_the_same_path` — and the estimator, the cadence, the
stale invariant), `tests/test_usage_meter_outside.py`, `tests/test_cost_meter_payload.py`
(the subtree, the sentence, the surfaces), `tests/test_ui_cost.py`. Existing cases must
pass unchanged: their resets move `resets_at` by a window, which is still a reset. The
`RESETS` constant and the `sample`/`series` helpers in `tests/test_usage_meter.py` are the
seam for the new ones.

Add to `tests/test_usage_meter.py`:

1. **The production case.** A monotonic rise 0.0 -> 87.0 over a 7-day span where every
   sample's `resets_at` is drawn from the four quoted values (1791882000.49662,
   1791882000.479002, 1791882000.321001, 1791881999.789849): `reset_count == 0`, one
   segment, `delta_points == 87.0`, `reliable is True`. Assert on `delta_points` and not
   only on `reset_count` — the reset count going to 0 with the delta still wrong would be
   half a fix.
2. **A genuine advance is still a reset.** `resets_at` + 18000 between two samples -> one
   boundary, `cause == "reset"`, the later segment starts at 0.0 (§4 rule 4 intact).
3. **Backwards past the tolerance.** `resets_at` - 3600 -> no boundary, `anomalies == 1`,
   `reliable is True` (an odd reading is not an unusable span).
4. **The tolerance edges.** +59s -> no reset; +61s -> reset. Pinned so a later change to
   `RESET_TOLERANCE_SECONDS` must be deliberate.
5. **Bound violation.** Construct a `SegmentSum` that trips each check (monkeypatch
   `_boundary` to return "reset" always; and one segment rising 0 -> 120 pct):
   `reliable is False`, the expected key in `unreliable`, `reset_count == max_resets`, no
   segment delta above 100.0, and the segments list NOT re-merged. Per key: the same
   `_boundary` patch over a 7-day span gives `max_resets` 34 for `five_hour` and 1 for
   `seven_day`.

Add to `tests/test_cost_meter_payload.py`:

6. **Sentence under low coverage.** 3% coverage over a span: the sentence contains
   `UNRELIABLE`, contains the coverage percentage, and contains NONE of `"lower bound"`,
   `RESIDUAL_LABEL`, `"unexplained"` or a `$` figure sourced from the meter; the measured
   Jarvis and outside dollars are still present. `implied_usd`, `residual_usd` and all
   three shares are `None`.
7. **Sentence between the thresholds.** 70% coverage, no bound tripped: a dollar figure IS
   printed, the coverage clause says "incomplete", and "lower bound" appears nowhere.
8. **Alerts and the daemon.** With an unreliable subtree, `alerts["outside"]` and
   `alerts["residual"]` are both `False`, `alerts["unreliable"]` is `True`, and
   `daemon.check_meter_residual` over that span writes no finding, no event and no inbox
   item (extends the existing case 43 fixture in this file).
9. **The gap-longer-than-a-window case.** High overall coverage with one 6h hole ->
   `"gap_longer_than_window"` in `unreliable`, no dollar figure.

`tests/test_ui_cost.py`: `/cost` with an unreliable subtree returns 200, shows the
"unreliable" marker, and shows no `$/point` figure.
