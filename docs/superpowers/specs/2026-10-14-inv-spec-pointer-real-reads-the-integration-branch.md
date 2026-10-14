# INV-SPEC-POINTER-REAL reads the integration branch, not the working tree

Work order wo-d40dfabe · GitHub issue 1044 · project jarvis_os

## 1. The problem

`invariants.check_spec_pointer_resolves` (`src/jarvis/invariants.py:5161`) judges whether a
feature order's `plan["design_doc"]` resolves by testing the LOCAL WORKING TREE:

```python
if not doc or (store.project_path / doc).exists():
    continue
```

and its candidate search `_tracked_with_basename` (`src/jarvis/invariants.py:5224`) asks the
same checkout, with `git ls-files -z` run at `cwd=root`.

For jarvis_os that checkout is the DEV checkout — hand-edited, routinely behind `origin`.
So the invariant's verdict is a fact about whether somebody has pulled, not about whether
the spec exists.

Evidence, 2026-10-14, after jarvis-0.11.7: a false INV-SPEC-POINTER-REAL was raised for
fo-f02d33af. Its spec
`docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md` is on
`origin/main` (commit 4611169, PR #1002) and present in the production checkout. The dev
checkout was 38 commits behind, the file was neither on disk nor in `git ls-files`, so the
probe took the zero-match branch and wrote the owed remedy "move or rename the file so the
stored path opens, or cancel the feature with `jarvis fo cancel fo-f02d33af`" — advice that
is wrong in both halves: the file needs no move, and the feature needs no cancelling.

Root cause: the ORACLE, not the message. Existence is being decided against a mutable,
per-machine working tree while every reader this invariant protects (`specs.spec_link`,
`ops.spec_section`, the feature agent, the plan validator) is about a document the fleet
shares. The mirror-image failure is equally real and already latent: a design_doc that
exists ONLY as an uncommitted or untracked local file passes `.exists()`, so a genuine dead
pointer is HIDDEN for exactly as long as that file sits in somebody's checkout.

## 2. The fix

Decided by Neo on question 1715; this section specs it and does not reopen it.

### 2.1 Drop the working-tree rung entirely

`check_spec_pointer_resolves` stops calling `(store.project_path / doc).exists()`. Existence
is judged on COMMITTED refs only, on the ladder `landing.committed_text`
(`src/jarvis/landing.py:324`) already pins:

1. the planner's branch, reached from `fo["plan_wo_id"]` via `store.get_work_order` — the
   same row `ops._planner_work_order` (`src/jarvis/ops.py:9635`) fetches for
   `committed_text`'s two existing callers;
2. `evidence.base_ref()` (`src/jarvis/evidence.py:757`), used at
   `src/jarvis/landing.py:348`, after a best-effort `landing._fetch_ref`
   (`src/jarvis/landing.py:359`).

Branch-before-main order is load-bearing and is NOT an implementation detail to tidy: per
`committed_text`'s docstring (`src/jarvis/landing.py:330-331`), in question 658 the older
revision was already on main while the newer sat on the planner's branch, so a main-first
ladder served stale text. Same hazard here: a spec committed on the planner's branch and
not yet merged must resolve, or every in-flight feature raises a violation.

### 2.2 A new existence helper in `landing`, not in `invariants`

New helper beside `committed_text` in `src/jarvis/landing.py`. It lives THERE because the
ladder lives there: a second copy of "planner's branch, then `base_ref` after a fetch" in
`invariants.py` is exactly the per-caller re-guess `evidence.base_ref`'s docstring names as
the failure mode (`src/jarvis/evidence.py:767-770`).

Two properties distinguish it from `committed_text`:

- It is an EXISTENCE test, so `git cat-file -e <ref>:<path>` — not reading the whole file.
  The invariant scans up to `SPEC_POINTER_ROWS` (`src/jarvis/invariants.py:5156` = 100000)
  feature-order rows; materialising spec text for each is waste.
- It refreshes the remote AT MOST ONCE PER SWEEP, via a memo set of already-refreshed refs
  threaded in BY THE CALLER (`check_spec_pointer_resolves` creates one empty set per
  invocation and passes it to every row). Not a time-based TTL: a TTL makes the behaviour
  depend on wall clock and on how long the sweep took, which is untestable and makes the
  fetch count unpredictable. The memo makes "one fetch however many rows" an exact
  property a test can assert.

`committed_text` itself is LEFT UNTOUCHED — its two text callers, `ops.submit_plan`
(`src/jarvis/ops.py:9796`) and `ops.refresh_plan_spec` (`src/jarvis/ops.py:9923`), want the
bytes and want their provenance string, and neither participates in a sweep.

### 2.3 Candidates come from the same ref

`_tracked_with_basename` (`src/jarvis/invariants.py:5224`) replaces `git ls-files` with
`git ls-tree -r --name-only <ref>` against THE SAME ref that answered the existence
question. Reason: the single-match branch AUTO-REPAIRS, writing `plan["design_doc"]` back
with `store.update_feature_order`. Resolving candidates from `git ls-files` would let the
repair repoint a shared database row at a path that exists only in one person's local
checkout — an unforced write of something false, and worse than the false positive it
replaces.

### 2.4 Nothing to read means no violation

When the ladder can answer NOTHING — no planner branch on disk AND `base_ref()` returns
`""` (a repo with no remote and no `main`) — the probe yields NO violation for that row and
moves on. A check that cannot run PASSES; it never refuses and never guesses (kn-774e8bae,
and the pinned never-fabricate ruling). This is also `landing.authored`'s existing posture
for rung 4 (`src/jarvis/landing.py:304-307`) and `committed_text`'s `None` contract.

Consequence, stated plainly rather than buried: a project with no integration branch is no
longer covered by this invariant at all. That is the accepted price. The alternative — fall
back to the working tree when the ladder is silent — reintroduces both failures in 1 for
precisely the repos where nobody can tell which answer was used.

### 2.5 No new catalog key and no new constant

No new bound is introduced, so there is nothing to make configurable. The fetch timeout is
landing's existing `timeout=20` (`src/jarvis/landing.py:371`) and the once-per-sweep memo is
a memo, not a bound. Neo's condition on question 1715 was that any bound WE introduce must
be a catalog key; we introduce none.

### 2.6 Rejected alternatives

1. **Keep `.exists()` and add the committed check as a fallback.** Fails the hide half: an
   uncommitted local file still short-circuits, so the invariant still cannot see a dead
   pointer in a dirty checkout. Also leaves the verdict machine-dependent.
2. **`git fetch` once per row, or a TTL-guarded fetch.** 100000 rows means up to 100000
   20-second network calls in a reconcile tick; a TTL makes the count depend on timing.
3. **Fix the message only — soften the owed remedy to "or pull".** Treats the symptom. The
   item is still raised, still flagged for attention, still needs acking, and the auto-repair
   can still repoint at a local-only path.
4. **Teach the invariant to run in the production checkout instead.** Moves the problem to
   "which checkout is canonical" and breaks every project that has only one.

## 3. Test obligations

The implementer TDDs these; each is a property of the new oracle, not of a message string.

1. `design_doc` on `origin/main` but ABSENT from the working tree raises NOTHING — the
   issue-1044 regression.
2. `design_doc` present ONLY as an uncommitted or untracked local file STILL raises — the
   hide half.
3. `design_doc` on the planner's branch and not yet on the integration branch raises
   NOTHING — §2.1 rung order.
4. Single-basename-match auto-repair repoints to a path that exists ON THE REF; a path that
   exists only locally is NOT a candidate — §2.3.
5. The remote is refreshed AT MOST ONCE however many feature-order rows are scanned — §2.2
   memo, asserted on the fetch call count.
6. No integration branch at all (no remote, no `main`) yields NO violation — §2.4.

## 4. Out of scope

- `tests/test_spec_location.py`, the tracked-file guard this invariant was written to
  complement, is unchanged.
- `committed_text`'s own signature and its two `ops.py` callers are unchanged (§2.2).
- The violation's wording, `subject_id` dedupe identity and the repair-vs-owed split are
  unchanged; only the predicate that triggers them moves.
