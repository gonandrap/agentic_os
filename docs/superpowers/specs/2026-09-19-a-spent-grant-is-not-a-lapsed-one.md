# A spent gate grant is not a lapsed one

Issue 491 · wo-c24e05e3 · 2026-09-19

## The defect

`ProjectStore.expire_approvals` swept an `approved` grant into
`status='expired', closed_as='lapsed'` on either of two conditions:

* `uses >= max_uses` — the grant was approved AND USED. The merge ran.
* `expires_at < now` — the grant was approved and nobody ever used it.

Those are opposite outcomes, and both then rendered as one word: `jarvis gate list`
printed `expired by neo`, and `/gates` badged them `– expired`, muted.

Measured in production on 2026-09-19: of 26 `auto_merge` gates, 23 were approved by Neo
in 9.8–35.8s with `uses=1/1` and their pull requests are merged on `main`. All 23 read as
`expired by neo`, indistinguishable from the 54+17 rows that genuinely were never decided
or never used. A working auto-merge pipeline read as Neo letting every merge lapse — a
cost paid straight out of the user's attention budget.

The precedent already existed: `closed_as='abandoned'` got its own display word in
spec 2026-09-12 §4–§5, on the argument that calling it "expired" alongside a lapsed grant
hides the outcome that matters. The spent case never got the same treatment.

## §1 The fix

**A fourth `closed_as`.** `expire_approvals` runs two sweeps instead of one: `spent`
(`uses > 0 AND uses >= max_uses`) first, then `lapsed` for the rest. Spent goes first so
a grant that is both spent and timed out reads as what actually happened to it. `uses > 0`
rather than `uses >= max_uses` alone, so a hypothetical `max_uses=0` row — never usable,
never used — cannot be filed as a success.

**One display key, three surfaces.** `ui.app.gate_display(row)` returns `closed_as` when
an `expired` row carries `spent` or `abandoned`, and the status otherwise; it is a Jinja
global, so `/gates` and the work-order page cannot drift apart. `cli._gate_display` is
its twin for `jarvis gate list`, and `test_gates` pins the two to the same keys.

* `/gates`: `✓ spent`, toned **ok** — a success, and the majority of decided gates —
  with `1/1 used` beneath it, which the page previously showed only for live grants.
* `jarvis gate list`: `☑ spent — approved by neo, then used 1/1`.
* The work-order page picks up `abandoned` and `spent` at the same time; it was keyed on
  the raw status and so had never shown either.

**A backfill, not just a going-forward fix.** `_backfill_spent_gates` re-files existing
`lapsed` rows with `uses > 0 AND uses >= max_uses`. Without it the 23 measured rows keep
reading wrong for ever. Narrow and idempotent, like `_backfill_abandoned_gates` beside
it: nothing else writes `closed_as='lapsed'`, and no verdict a reviewer reached is
touched.

## What is deliberately unchanged

`status` stays `expired`. It is the status that means "decided once, cannot clear a
command now", and `usable_grant` already refuses both cases for their own reasons; a
fifth status would need handling in every gate surface to say something `closed_as`
already says. Same call spec 2026-09-12 made for `abandoned`.

"Spent" says the gate opened and the command was allowed to run — `consume_grant` is
called at that moment. It does not claim the command succeeded, and no surface says it
did.
