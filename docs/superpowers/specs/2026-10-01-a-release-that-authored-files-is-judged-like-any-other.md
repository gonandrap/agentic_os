# A release that authored files is judged like any other

Work order wo-f4e879b8. GitHub issue #900. Supersedes the release exemption narrowed in
`docs/superpowers/specs/2026-09-29-a-release-blocked-by-a-red-main-retries-itself.md` §2.

## The problem

The panel exemption is keyed on the KIND of order, and its own stated ground is a claim
about the ARTIFACT — a claim that is false for some release orders.

`src/jarvis/ops.py:7554`:

```python
return cfg is not None and cfg.enabled and not release.is_release_order(wo)
```

`release.is_release_order` (`src/jarvis/release.py:88`) tests one thing: does `metadata`
carry a `release.BATCH_KEY` list. The docstring above it (`src/jarvis/ops.py:7543-7553`)
gives the reason for the exemption: a release order "authors no files and stages no tag",
so `evidence.nothing_to_judge` escalated the round with "nothing to review" and
`autoreview.HELD_PANEL_GAVE_UP` put a hold on the assumption that not even Neo could
clear. That reasoning is sound and stays — it is just not coextensive with the predicate
that encodes it.

Evidence that the premise can be false: **wo-33e1d0b4**. A release order (metadata carries
`release.BATCH_KEY`) that changed `scripts/shipit.sh`, opened PR 899, and finished with
`jarvis wo finish --pr`. Measured now: parked `waiting_pr_merge`, PR OPEN / MERGEABLE /
CLEAN, all three checks SUCCESS, 11.9 hours elapsed, no attention item (by design —
`waiting_pr_merge` raises none).

It can never merge, and the hold is unreachable by construction:

- `finish` took the exempt branch at `src/jarvis/ops.py:6212`, so no round was ever opened.
- `automerge.decide` conditions 4 and 5 require `validated_head` — a round settled
  `passed` with a recorded commit. With no round at all it returns
  `_held(HELD_NOT_PASSED, "the panel has never judged this work order")`
  (`src/jarvis/automerge.py:405-408`).
- Nothing else opens a round for a `waiting_pr_merge` order. `_validates_on_review`
  (`src/jarvis/ops.py:7538`) rides the same predicate, and `jarvis validation force`
  needs the order to have delivered — which it has — but that is a USER action, not a
  resolution. Left alone, the pull request sits open for ever.

Root cause, stated plainly: the predicate tests the order's kind as a proxy for a property
of its submission. A release order that authors a diff falsifies the proxy. This is the
root cause, not a symptom — the proxy itself is the defect.

## The fix

Test the premise, not the kind. Ratified by Neo, question 1169; not open for
re-litigation.

A release order is exempt from the panel **only when it delivered no pull request**. One
that finished with `--pr` authored files, so `nothing_to_judge` does not apply to it and it
submits like any code-bearing order.

### 1. The predicate

`validation_applies` (`src/jarvis/ops.py:7542`) becomes:

```python
return (cfg is not None and cfg.enabled
        and not (release.is_release_order(wo) and not str(wo.get("pr_url") or "")))
```

The pull request is read from the **work order dict** (`wo["pr_url"]`), not from a
separate argument and not from the store. This keeps it one predicate with one source:
every caller already holds a work order dict, and a second source would be a second answer
to "was there a diff". Update the docstring: the exemption's ground is the ABSENT pull
request, and `is_release_order` only narrows which orders may claim it.

Rejected: a new `release_exempt(wo)` helper beside the predicate. It splits the question
across two symbols that both have to be consulted at every call site, which is how the
current defect got in — a site read `is_release_order` and never the premise it stands for.

Rejected: reading `_release_effects(store, wo_id)` (the staged-release marker) instead of
the pull request. That answers "did the release ship", a different question, and is already
`defer_red_release`'s. A release can both ship a tag AND author a diff; the diff is what a
seat reads.

### 2. The ordering hazard at `finish`

`src/jarvis/ops.py:6212` calls `validation_applies(cfg, fresh)` BEFORE the `pr_url` column
is written. `land_when_cleared(store, fresh, pr_url, ...)` at `src/jarvis/ops.py:6231` is
what writes it (through `land_finished`). So `fresh["pr_url"]` is empty on this path even
for an order delivering a pull request, and the naive change would be a no-op for the live
case.

`finish` must pass the url it already holds in its local `pr_url`:

```python
if validation_applies(cfg, {**fresh, "pr_url": pr_url}):
```

Call sites and what each reads:

| Site | Dict | Needs the overlay? |
|---|---|---|
| `src/jarvis/ops.py:6212` (`finish`) | `fresh`, pre-write | **Yes** — local `pr_url`, as above |
| `src/jarvis/ops.py:7538` (`_validates_on_review`) | `store.get_work_order(wo_id)` | No — the column was written by the earlier landing |
| `src/jarvis/ops.py:7583` (`_land_after_acceptance`, local `cleared`) | see §3 | No overlay, but **not** the blanked dict |

Rejected: writing the `pr_url` column before the submission branch. The write belongs to
`land_finished`, and hoisting it would put a pull request on an order whose status the
join has not yet decided — the poll selects on that column.

### 3. The two `panel_cleared` sites move in lockstep

Both currently say `panel_cleared=release.is_release_order(...)`:

- `src/jarvis/ops.py:6231-6232`, inside `finish`.
- `src/jarvis/ops.py:7583`, in `_land_after_acceptance` (local `cleared`, passed at
  `:7587`).

Both become `not validation_applies(cfg, <the same dict the submission branch read>)`.

They must move WITH the predicate, in the same change. `panel_cleared=True` tells the join
"the panel's half is settled, do not re-read the round" (`land_when_cleared`,
`src/jarvis/ops.py:5077`). A release order with a pull request now OPENS a round; telling
the join the panel is cleared would land it `completed` with a diff no seat read — the
exact inverse of the defect being fixed, and worse, because it merges rather than stalls.
A predicate change without these two is a regression, not a partial fix.

`_land_after_acceptance` has `cfg` in scope: it is a parameter
(`src/jarvis/ops.py:7557-7558`). No signature change.

**Which dict the predicate reads there.** `src/jarvis/ops.py:7580-7581` deliberately blanks
the column for a non-awaiting order:

```python
if not _awaiting_merge(fresh):
    fresh = {**fresh, "pr_url": ""}
```

That blanking exists so a review does not put a CLOSED pull request back in front of the
poll. It must NOT feed the panel question: a release order in `needs_review` with a closed
pull request still authored files, and reading the blanked dict would make
`_validates_on_review` (which reads the unblanked `store.get_work_order(wo_id)` at
`:7538`) open a round while `cleared` told the join the panel was settled — the two halves
of one join disagreeing. So the predicate reads the **unblanked** row. Compute it before
the blanking, or re-fetch:

```python
cleared = not validation_applies(cfg, store.get_work_order(wo_id))
```

The blanked `fresh` continues to be what is handed to `land_when_cleared` as the work
order. Note the asymmetry is intentional: `_validates_on_review` additionally requires
`latest_validation_round(...) is None`, so an order with an existing round does not
re-submit — but `cleared` is `False` there, and the join correctly re-reads that round.

### 4. What is NOT touched

**`automerge.decide` — byte-identical.** The obvious alternative is an exempt branch in
`decide` so a release order merges without a `validated_head`. Rejected: that module's
docstring promises it never lands a diff no seat read, and a release order with a pull
request is precisely a diff. The fix belongs where the wrong question is asked
(`validation_applies`), not where the right answer is enforced.

**`_validates_on_review` (`src/jarvis/ops.py:7524`) — no edit.** It composes
`validation_applies`, so it inherits the new behaviour by construction: a release order
with a pull request parked in `needs_review` and never judged now submits on acceptance.
That is the catch-up path the function exists for, and the new population is exactly the
one the defect created.

**`defer_red_release` (`src/jarvis/ops.py:4915`) — no edit.** Its
`release.is_release_order` call at `:4954` answers a different question: should a release
order that delivered NO release wait for a green base. The pull request is irrelevant to
it. Ordering note: it runs at `src/jarvis/ops.py:6208`, before the submission branch, so a
deferred order re-finishes later and submits then — unchanged.

**`daemon.py:2136` (no-validator path) and `daemon.py:2457` (`_void`) — unrelated, stay
`panel_cleared=True`.** Neither is a release question. Each has just closed the round it
owns (`failed` / `void`) and is telling the join not to re-read a row it wrote, which would
read as "in flight". Literal `True` is correct for both regardless of the order's kind.

## The behaviour the tests must prove

Home: `tests/test_validation_release_skip.py` (already owns the exemption; its
`release_order` helper at `:34` and `stage_release` at `:46` cover the setup). Update its
module docstring — the exemption is now conditional.

1. **`test_release_order_without_pr_opens_no_round`** — release order, `finish` with no
   `--pr`: no validation round, lands as today. The existing exemption cases must still
   pass unchanged.
2. **`test_release_order_with_pr_opens_a_round_at_finish`** — release order, `finish --pr`:
   exactly one round on record. Fails before the fix because `fresh["pr_url"]` is empty at
   `src/jarvis/ops.py:6212` — this is the test that catches a fix missing the §2 overlay.
3. **`test_release_order_with_pr_is_not_told_the_panel_is_cleared`** — same order ends
   `validating`, not `completed`/`waiting_pr_merge`. Guards §3; fails if the predicate
   moved and the `panel_cleared` sites did not.
4. **`test_release_order_with_pr_submits_on_review_acceptance`** — release order parked
   `needs_review` with a pending assumption and a stored `pr_url`, accepted: one round
   opens and `cleared` is false. Covers `_land_after_acceptance` reading the unblanked row.
5. **`test_automerge_decide_unchanged`** — a release order with a pull request and no
   passed round still gets `HELD_NOT_PASSED`; `src/jarvis/automerge.py` has no diff in
   this change.

Not covered here, on purpose: nothing re-opens a round for wo-33e1d0b4 retroactively. It is
already delivered and parked, so `jarvis validation force wo-33e1d0b4 --reason "…"` is the
remedy for the live order, and it is a user action outside this change.
