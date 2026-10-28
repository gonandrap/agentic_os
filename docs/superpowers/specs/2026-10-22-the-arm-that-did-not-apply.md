# The arm that did not apply

Issue 1227. Extends 2026-10-15-the-hour-on-worker-turns-only.md §4–5 and closes the gap
kn-dbf6f779 left behind.

## The problem

`wo_turns.cache_ttl` records the arm a turn was LAUNCHED on — intent. Only the
transcript's `ephemeral_1h` / `ephemeral_5m` split records the rate the turn PAID. Issue
1187 (an env flag the CLI ranked below a user settings key) ran for ~1.6 days of armed
traffic because nothing compared the two: every turn was stamped `1h` while paying 5m.

Neither existing guard can see it. `Daemon.check_cache_ttl` alarms on the opposite case
only — 1h writes nobody asked for — and `inspection.one_hour_writes` skips any transcript
that does not MENTION the 1h key, so a 1h-armed session that bought no hour is invisible
to it by construction.

## The fix

One new alarm kind, `inspection.CACHE_ARM_MISSED_ALARM` = `cache-arm-missed`, raised from
the pass that already exists.

**The oracle is the pair, per turn.** `wo_turns.usage_json` carries the turn's own
`cache_1h` / `cache_5m` beside `cache_ttl` (it is what `jarvis cost <wo-id> --json` prints
as `turn_rows[]`), so the check reads the RECORD and needs no new column, no transcript
walk and no second policy.

`ProjectStore.turn_arm_rates(since)` returns
`(wo_id, seq, cache_ttl, started_at, cache_1h, cache_5m)` for every stamped turn with
recorded usage since the window opened. `Daemon._arm_rate_rows` builds the fleet-wide list
across `self.catalog.projects` (the `_turn_arms` shape), prepending the project name; an
unreadable project is logged and skipped.

`inspection.arm_misses(rows)` classifies one turn at a time:

| turn | verdict |
|---|---|
| arm `cli` (or unstamped) | **skipped** — `cli` claims no rate, so none can be missed |
| arm `1h`, `cache_1h == 0`, `cache_5m > 0` | **miss** of `cache_5m` tokens — the arm never reached the CLI |
| arm `5m`, `cache_5m == 0`, `cache_1h > 0` | **miss** of `cache_1h` tokens — the hour nobody asked for |
| neither side reported | **skipped** — the CLI said nothing about the rate, and that is not evidence (kn-dbf6f779) |
| the armed side nonzero | **skipped** — the arm applied |

`inspection.arm_miss_alarms` sums the missed tokens over the window and raises ONE alarm
at `InspectConfig.alarm_cache_arm_missed_tokens` (default 50,000, the
`alarm_cache_1h_dispatched_tokens` figure), naming the count on each side, the hour
premium the 5m-armed half paid, and the three biggest turns. Both directions are a DEFECT
IN THE OS: the general rule kn-dbf6f779 states is that an env variable the OS sets must
outrank every user-owned key for the same behaviour.

`check_cache_ttl` appends these to `hour_alarms`' list and raises them through the same
carrier, dedupe and inbox path (`CACHE_1H_INBOX_TITLE` gains the kind). Because the new
check reads no transcripts, the transcript scan's `OSError` branch no longer returns
early: it logs, and the record-only half still runs.

## Why an alarm and not an invariant

`invariants.check_os` has exactly one production caller, `ops.run_doctor` (kn-fb5fa0f4),
so a post-condition there reaches nobody until a person types `jarvis doctor`. The issue
asks for the arm to be noticed within a day, which means the daemon pass.

## Out of scope, stated

- No new column, no transcript parsing, no per-turn alarm: one aggregate per window, like
  the 1h pair.
- A turn whose usage was never recorded (`usage_json IS NULL`) is not judged. Readers
  backfill it lazily from the outfile; until then there is no rate to compare.
- The 5m-armed direction overlaps `cache-1h-dispatched` by construction. It is kept
  because that alarm needs the transcript tree and this one does not, and because the
  issue asks for the mirror explicitly.
