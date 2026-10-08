# A feature round must judge a head that contains its children

Work order wo-3761eb35. GitHub issue #952. Neo question 1394 settled the mechanism; §6 lists
the rejected alternatives.

## The problem

**A feature-level validation round judges whatever commit the project checkout's
`origin/main` was last fetched at, which can predate a child's merge. The panel then
reports the child's work as missing and rejects — a false blocker that spends a round.**

The chain, as it stands in this worktree:

1. `evidence.collect_feature` (src/jarvis/evidence.py:621) computes the judged range as
   `base_sha...default_branch_head(project_path)` (line 664).
2. `evidence.default_branch_head` (src/jarvis/evidence.py:705) is
   `git rev-parse <base_ref(repo)>`; `base_ref` (src/jarvis/evidence.py:723) resolves
   `refs/remotes/origin/HEAD`, else `origin/main`, else `main`. Every rung is a LOCAL ref
   read. **Nothing in the path fetches.** `ops.submit_feature_for_validation`
   (src/jarvis/ops.py:6038) does not; `ops.collect_feature_evidence`
   (src/jarvis/ops.py:6004) does not; `Daemon._route_to_validation`
   (src/jarvis/daemon.py:1447) does not.
3. So the head is as stale as the last unrelated `git fetch` anyone happened to run in that
   checkout.

Measured, fo-ac00376e round 1, fingerprint `1ebc33a71119fc23`:

- judged range `1dffd9e..4e655b0`
- `4e655b0` is 2026-10-01 19:12 PDT
- child wo-8fd40f1a's PR #893 merged as `3704a0b` at 19:31 PDT, nineteen minutes later
- `3704a0b` is **not** an ancestor of `4e655b0`
- the panel rejected with `wo-8fd40f1a files are absent from the integrated change`

With issue #881 on top, that parked the feature five days.

The root cause is the missing fetch. The second-order cause is that nothing asserts the
precondition the collector silently assumes: *the head contains every child's merge*. A
fetch alone narrows the window; it does not close it, because a merge landing between the
fetch and the `rev-parse` is still invisible, and a child merged somewhere other than the
default branch never appears at all.

### Second defect, same round: the judged commit is not recorded

`validation_rounds.head_sha` is never written for a feature round, so
`jarvis validation show <fo-id>` prints "commit not recorded" for every round a feature has
ever had. `ProjectStore.set_validation_head` (src/jarvis/project_store.py:4835) exists and
has exactly one caller: the work-order path at src/jarvis/daemon.py:2205. The column exists;
nothing writes it from the feature path.

The consequence is not cosmetic. A rejection that recorded no commit cannot later say what
it rejected — which is precisely the evidence this work order needed to diagnose the first
defect, and had to reconstruct from the round's fingerprint and `git log` timestamps by
hand.

## The fix

Five pieces. Numbered so tests and code comments can cite them.

### 1. Unconditional fetch, in `ops.submit_feature_for_validation`

In `ops.submit_feature_for_validation` (src/jarvis/ops.py:6038), **before** the
`collect_feature_evidence` call at line 6055:

```python
from . import branchproof
ref = evidence_mod.base_ref(project_path)
if ref and not branchproof.fetch(project_path, ref):
    log.warning(...)   # not fatal
```

`branchproof.fetch` (src/jarvis/branchproof.py:118) already takes refs, already strips an
`origin/` prefix, already validates against `REF_RE`, and already returns `False` with a
warning when git fails. `base_ref` is already public for exactly this reason — it is the one
ladder that decides which branch is the default, and a fetch that guessed `main` while the
collector resolved `origin/HEAD` would fetch a different branch than it judges. An empty
`base_ref` means rung 4, no default branch: skip the fetch, the collector returns an empty
packet anyway.

**A failed fetch is logged, not fatal.** The round opens on the best local answer, exactly as
today. A project with no network must still be able to validate, and §2 is the check that
catches the stale case regardless of why the fetch failed.

**WHY IN `ops` AND NOT IN THE DAEMON.** `submit_feature_for_validation` has two callers —
`Daemon._route_to_validation` (round 1) and `jarvis fo submit` (every round after). Both need
a fresh head and neither has any reason to want a stale one, so the fetch belongs at the
single point they share. Putting it in the daemon would leave `jarvis fo submit` judging a
stale head forever, which is the common case for rounds 2+.

**NOT in `evidence.collect_feature`.** That module runs no network and reads no store; that
is its whole value. A collector that fetched could influence what it reports on.

### 2. The ancestry precondition, in `Daemon._route_to_validation` only

In `Daemon._route_to_validation` (src/jarvis/daemon.py:1447), after the
`cfg.enabled and cfg.feature_units` check and after the "already has a round" branch, and
before the `ops.submit_feature_for_validation` call: for every child the feature is about to
be judged on, require that the child's merge commit is an ancestor of the head
`collect_feature` will judge.

Pieces, all of which exist:

- the head: `evidence.default_branch_head(project.path)` — the same function the collector
  calls, read after §1's fetch, so the check and the collection answer about one commit.
- the child's merge commit: `Daemon._merge_commit_of` (src/jarvis/daemon.py:8227). It reads
  the `pr_merged` event's `merge_commit`, falls back to its own `MERGE_COMMIT_EVENT`
  back-fill row, and only then spends ONE `gh pr view` — once ever per child, written back as
  an event, not once a tick.
- the ancestry test: `branchproof.is_ancestor(project.path, merge_sha, head_sha)`
  (src/jarvis/branchproof.py:230). `False` when git cannot answer, which reads as "not
  proven", never as "fine".

Children in scope are the ones `settle_features` counted: `store.feature_children(fo["id"])`
minus `superseded`, status `completed`. A child with no `pr_url` and no merge event
contributes nothing to check — it delivered without a pull request — and does not defer.

**IF ANY REQUIRED CHILD'S COMMIT IS MISSING FROM THE HEAD, `_route_to_validation` RETURNS
`True` AND OPENS NO ROUND.** `True` is correct and `False` would be a bug:
`Daemon.settle_features` (src/jarvis/daemon.py:1265, line 1374) reads the bool as
`if self._route_to_validation(...): continue` and otherwise calls `self._complete_feature`.
Returning `False` would COMPLETE the feature unjudged — strictly worse than the false
rejection this spec is fixing. `True` means "dealt with, leave it alone", and the feature
stays `executing`, so the next reconcile tick re-enters this branch and asks again. The
docstring's two cases become three; say so there.

**The new code's own failures defer too.** Any exception from the precondition (a `gh`
error that `_merge_commit_of` did not swallow, a store read) is caught and treated as
"cannot prove" — defer, return `True`. It must NOT fall into the existing
`except Exception: return False` arm around `ops.submit_feature_for_validation`, which
completes the feature. That pre-existing arm is not touched here (§7).

**No attention flag while deferring.** A feature waiting for a merge to become visible is the
system working, and the OS's rule is that waiting never flags. The flag is §3's job.

**WHY THE DAEMON AND NOT `ops`.** Only the daemon has a next tick. A deferral is "ask again
in a minute", and a function with no retry loop behind it cannot defer — it can only refuse.
`jarvis fo submit` is a manager's explicit act: it must never silently decline to open the
round the manager asked for, because there is nothing to re-drive it and the manager is left
waiting on a round that does not exist. So `fo submit` gets §1's fresh head and opens its
round as it does today.

### 3. The deferral is bounded in MINUTES, and expiry flags attention

Measured since the child's merge, not counted in ticks: ticks are an implementation detail
whose period the user never set, and a bound in ticks silently changes meaning when the
reconcile interval does. The timestamp is the `pr_merged` / `MERGE_COMMIT_EVENT` row's `ts`
(epoch seconds, `store.events_of_kind`, same convention as
`invariants._parked_minutes`/`retry_hold`): `time.time() - float(row["ts"]) >=
minutes * 60`.

On expiry, for that feature, the daemon:

1. still opens no round,
2. still returns `True` (the feature must not complete on an unjudged head either),
3. calls `ProjectStore.flag_feature_attention(fo_id, reason)`
   (src/jarvis/project_store.py:3007).

The reason is a template beside `FEATURE_CHILD_FAILED` / `FEATURE_CHILD_CANCELLED` in
src/jarvis/invariants.py:532 — every reason the OS puts in front of the user is written in
one file — and must name three things, because each points at a different remedy:

```
FEATURE_CHILD_NOT_INTEGRATED = (
    "{id}'s merge commit {commit} is not in {head}, the commit on the default branch a "
    "review would judge — so a panel would report its work as missing. Nothing has been "
    "judged.")
```

**Why bounded at all.** A child merged into a sibling branch rather than the default branch
(kn-e67a51d5) will never become an ancestor of that head. Unbounded, the feature defers for
ever and parks silently — the exact failure mode of issue #881, re-created by the fix for
#952.

Re-entry is idempotent: the same tick-level check re-writes the same `needs_attention` /
`attention_reason` values, which is a no-op update. `ops.submit_feature_for_validation`
already clears the flag (src/jarvis/ops.py:6067) if the merge later becomes visible and the
round does open.

One timeline event per distinct state, via `ops.feature_event` — kind
`validation_defer`, payload `{child, commit, head, waited_minutes, expired}` — written only
when `(child, commit, expired)` differs from the newest such event already on record
(`ops.feature_events_of_kind`). A per-tick event would write a row a minute for fifteen
minutes and then for ever.

### 4. The bound is a catalog key on `ValidationConfig`

In src/jarvis/catalog.py, beside the other validation numbers:

```python
# How long the reconciler may wait for a merged child's commit to appear on the default
# branch before it stops deferring the feature's round and asks the user. §3 of
# docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md
DEFAULT_VALIDATION_FEATURE_MERGE_WAIT_MINUTES = 15
```

and the field on `ValidationConfig` (src/jarvis/catalog.py:556):

```python
feature_merge_wait_minutes: int = DEFAULT_VALIDATION_FEATURE_MERGE_WAIT_MINUTES
```

parsed in `_parse_validation` (src/jarvis/catalog.py:1715) with that block's ordinary
field-level fallback, the same shape as `max_rounds` and `diff_chars`:

```python
feature_merge_wait_minutes = int(
    raw.get("feature_merge_wait_minutes", base.feature_merge_wait_minutes))
if feature_merge_wait_minutes < 0:
    raise _err(f"{where}.feature_merge_wait_minutes must be >= 0")
```

`base` is the fleet answer when a project names nothing and the shipped default when the
fleet names nothing either, so `os.validation` sets it fleet-wide and a project overrides it
alone. `0` is legal and means "never defer": check once, flag immediately. Reads as
`jarvis config set <project> validation.feature_merge_wait_minutes 30 --reason "..."`.

**NOT a module-level constant.** `ops.REBIND_MAX` is a constant because it bounds a loop the
OS runs against itself; this bounds how long the OS waits on GITHUB's propagation and on the
project's merge habits, which differ per project — the same thing that made every
`inspect.alarm_*_minutes` a key.

`_route_to_validation` reads it off the `cfg` it already holds (`project.validation`), so no
new config plumbing.

### 5. Record the judged commit on a feature round

Two edits.

**(a) `evidence.judged_head` must answer for a feature packet.** Today
(src/jarvis/evidence.py:268) it returns `""` for anything whose `source` is not
`pull_request`, and a feature packet's `source` is `"worktree"` with `pr` empty — so it reads
`packet.pr["head_sha"]`, which a feature packet has not got, and the function returns `""`
unconditionally. The correct expression for `unit == "feature"` is **`packet.head`**: on the
feature path that field holds the output of `default_branch_head`, i.e. a resolved commit sha,
and not the `headRefName` branch name the docstring's warning is about. So:

```python
if packet.unit == "feature":
    return str(packet.head or "")
if packet.source != "pull_request" or not packet.pr:
    return ""
return str(packet.pr.get("head_sha") or "")
```

The work-order arm is untouched, so `""` stays the fail-closed value there and auto-merge
behaviour does not move. The second caller, `validation._history_section`
(src/jarvis/validation.py:428), improves for free: a feature seat is told which commit it is
reading instead of nothing.

**(b) Write it from `Daemon._validate_feature`** (src/jarvis/daemon.py:2879), immediately
after the `ops.collect_feature_evidence` call at line 2909 and BEFORE the escalate/void
guards at lines 2953-2977 — the same position and the same reason as the work-order path at
src/jarvis/daemon.py:2205: it is a fact about the packet, written before any verdict exists,
so an escalation or a rejection can still say what it was about.

```python
store.set_validation_head(round_id, evidence_mod.judged_head(packet))
```

`evidence_mod` is already imported there (line 2894).

**No schema change.** `validation_rounds.head_sha` exists and `""` already means "not
recorded" for every pre-migration row.

**No fetch in `_validate_feature`,** deliberately. This is the second collection of the same
feature, and the head it resolves must be the head the round was fingerprinted against in
§1. Not fetching is what keeps the two equal: a remote that moved after §1's fetch cannot
change a local ref nobody re-fetched. So the commit recorded here is the commit the seats
read, which is the whole claim `head_sha` makes.

### 6. Rejected alternatives

1. **Fetch only, no ancestry check.** The obvious fix, and it is the one a reviewer will
   propose. It shrinks the race to the milliseconds between `git fetch` and `git rev-parse`
   instead of closing it, and it is silent on the case that actually parks a feature: a child
   whose merge is not on the default branch at all. A fetch cannot distinguish "nothing
   merged yet" from "merged somewhere else".
2. **Ancestry check in `ops.submit_feature_for_validation`, so both callers get it.**
   Symmetrical with §1 and wrong: `ops` cannot defer. The only outcomes available there are
   "open the round anyway" (today's bug) and "raise", and raising out of `jarvis fo submit`
   makes a manager's explicit resubmission fail on a condition that would have cleared by
   itself in a minute.
3. **Defer for N ticks instead of N minutes.** Cheaper to implement — no timestamp read —
   and it ties a user-visible wait to the reconcile interval, so changing the interval
   silently changes how long a feature parks. §3.
4. **Unbounded deferral.** Correct whenever the merge is merely slow, and it re-creates
   #881's silent park for the sibling-branch case (kn-e67a51d5). The user hears nothing.
5. **Flag attention while deferring, instead of only on expiry.** Would mean the OS asks for
   a person during the ~2 minutes GitHub and the merge poller normally take, on every
   feature, which is how an attention list stops being read.
6. **Add a `head_sha`-equivalent column for features, or widen `judged_head`'s PR arm.** The
   column already exists and takes a sha; the work needed was one branch for `unit ==
   "feature"` (§5a). A new column would make `jarvis validation show` read two fields for one
   fact.
7. **Hold the round until a child's PR is confirmed merged via `gh` on every tick.** Already
   available through `_merge_commit_of`'s back-fill, and making it per-tick costs one API call
   per child per minute for a condition a local `merge-base --is-ancestor` answers for free.

## Out of scope

- **The range BASE is the feature's creation point, not the children's merge-base.** Issue
  #952's second observation, and real: the measured round spanned 272 files and ~58K lines,
  most of it unrelated features that landed on `main` while fo-ac00376e was executing. It is
  a separate defect with a separate fix (what `base_sha` should be, and whether an existing
  feature's can be recomputed) and nothing here changes it. A round fixed by this spec is
  still too wide; it is no longer wrong about its own children.
- **The pre-existing `except Exception: return False` arm** around
  `ops.submit_feature_for_validation` (src/jarvis/daemon.py:1489-1493), which completes a
  feature when the round could not be opened. Suspicious for the reason §2 gives, but
  changing it alters settlement for every failure mode, not just this one. §2's new code
  routes around it rather than through it.
- Work-order rounds. The work-order path fetches as part of its PR read and already records
  `head_sha`; nothing in §1-§5 touches it.

## Tests the implementer must write

In tests/test_feature_validation.py unless noted.

1. **§1 — fetch is attempted before collection.** Patch `branchproof.fetch` and
   `evidence.collect_feature`; assert `fetch` was called with the project path and
   `base_ref`'s answer, and that it was called first. Second case: `fetch` returns `False`
   and the round still opens (logged, not fatal).
2. **§2 — a child whose merge commit is not an ancestor defers.** Feature with one
   `completed` child whose `pr_merged` event names a commit not reachable from the default
   branch head. Assert: `_route_to_validation` returns `True`, `store.validation_rounds(
   fo_id=...)` is empty, status is still `executing`, `needs_attention` is 0.
3. **§2 — `settle_features` does not complete a deferred feature.** Drive the whole tick, not
   `_route_to_validation` directly, and assert the feature is neither `completed` nor
   `validating` — this is the test that would have caught a `False` return.
4. **§3 — past the bound, attention and still no round.** Same fixture with the `pr_merged`
   event's `ts` set `feature_merge_wait_minutes + 1` minutes in the past. Assert
   `needs_attention` is 1, `attention_reason` contains the child id, the merge commit and the
   judged head, and that `validation_rounds` is still empty.
5. **§3 — one event per state.** Two consecutive ticks in the deferring state write ONE
   `validation_defer` event; the tick that crosses the bound writes a second with
   `expired: true`; a third tick writes nothing more.
6. **§2 — all children integrated opens the round as today.** Merge commits reachable from
   the head: round 1 opens, status `validating`, `validation_submitted` event present. This is
   the no-regression test for every existing feature-validation test's assumptions.
7. **§5 — `head_sha` is written and is the commit actually judged.** Run a feature round with
   an injected validator; assert `get_validation_round(...)["head_sha"]` equals
   `default_branch_head(project_path)` and equals the packet's `head` the validator was
   handed. Also assert it is written on the ESCALATE paths (null `base_sha`, empty diff), not
   only on a judged one.
8. **§5 — `judged_head` unit test** in tests/test_evidence.py (or wherever `judged_head` is
   covered today): feature packet returns `packet.head`; work-order worktree packet still
   returns `""`; work-order PR packet still returns `pr["head_sha"]`.
9. **§4 — the key resolves per project and falls back fleet-wide**, in tests/test_catalog.py:
   `os.validation.feature_merge_wait_minutes: 30` with a project naming nothing yields 30; a
   project naming 5 yields 5 while a sibling keeps 30; neither naming it yields
   `DEFAULT_VALIDATION_FEATURE_MERGE_WAIT_MINUTES`; a negative value raises `CatalogError`
   naming the key; `0` parses.
