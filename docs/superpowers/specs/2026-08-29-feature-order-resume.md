# Resuming a failed feature order

2026-08-29 · from wo-9d6bba51 · Neo question 177

## The bug

`Daemon.settle_features` only ever inspects `executing` feature orders. That is
deliberate — it is what makes "flag once, at feature level" true by construction, with no
dedupe set — and it also makes `failed` **terminal**. A child that ends badly settles the
feature; nothing re-derives the feature after that, ever.

So a child that *recovers* leaves the feature failed for good. Three ways in:

- a wrongly-flagged failure, corrected later (fo-e353491c: `wo-2df8828c` was failed by a
  bug in background-script handling under `claude -p`, and the work had in fact landed as
  PR 146);
- `jarvis wo done` on a failed child;
- a retry that succeeds.

fo-e353491c sat `failed` for a fortnight showing `12/12 done` and a reason naming a child
that was `completed`. The only route out was an edit to the production database.

## The rule

A child may now be **superseded**: the user has answered for its failure. A superseded
child settles its feature **neither way** — it no longer fails the feature, and it no
longer counts towards completion. A feature whose every child is superseded therefore
completes, which is right: nothing is outstanding.

The record lives in `feature_orders.metadata` under `SUPERSEDED_CHILDREN_KEY`, as
`[{"wo_id", "ts", "note"}]` — ids and reasons in one key so the two cannot disagree.

**Why metadata and not a feature event.** `ops.feature_event` writes to the feature's
project manager order and returns `False` when there is none, which is every feature
planned while `os.validation.enabled` was false — including the one that motivated this.
A record that silently fails to be written for the exact case it exists for is not a
record.

**Why annotated, not filtered.** `ProjectStore.feature_children` marks each child
`superseded` and returns them all. Billing, cancellation and the child tree still want the
row, and a superseded child that vanished from the tree would look like one that never
ran. Only the settle rule reads the flag.

## Two ways back

**`INV-FEATURE-FALSE-FAILURE`** (`invariants.py`) — automatic. A `failed` feature with
children and no *live* dead child goes back to `executing` and its flag is cleared. It
decides nothing: the next `settle_features` tick completes it or opens a validation round,
exactly as it would have the first time. Repaired on the daemon tick rather than behind
`jarvis doctor --repair`, because the state admits one reading and the failure mode is
silence.

**`jarvis fo resume <id> [--fix "…"]`** (`ops.resume_feature_order`, and a textarea on the
feature-order page) — the user, for a child that is still dead. Supersedes every dead
child, reopens the feature, and files `--fix` as a new child work order. `--fix` is
optional even when a child is dead: sometimes the honest answer is that the feature no
longer needs that work.

The two share `invariants.dead_feature_children`, which is also what `settle_features`
fails on. A settler and an invariant that disagreed about what a dead child is would flap
the feature on every tick.

`failed` only. `cancelled` was the user's own decision, and reversing it is a different act
with different consequences for the children they stopped.
