# A `deployed:` precondition clears itself

Issue 1104. Extends `docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md`
§2.6 with the one derivable hold that spec did not have.

## 1. The problem

`wo-0931ac4a` was filed `--depends-on wo-19e58248` to read the cache-TTL A/B **off the live
fleet**. `wo-19e58248` completed when its PR merged — `c507208` is an ancestor of
`origin/main` — so the edge cleared and the reading was dispatched. The fleet runs the
production tag `jarvis-0.11.14`, which does not contain `c507208`: on the production CLI
`jarvis config get jarvis_os worker.cache_ttl` answers `not a known setting`. Step 1 of the
order was impossible and a worker turn was spent proving it.

A `depends_on` edge clears at COMPLETE, and complete means merged to `main`. The fleet runs
only what the last release tagged and `shipit` deployed. The graph had no vocabulary for
"in a released tag production is running", and `--blocked-by` free text could express the
wait but nothing derived or cleared it: the user had to run `jarvis wo unblock`.

## 2. The fix

### 2.1 One new ref form, no new flag

`--blocked-by deployed:<wo-id|sha>`. `preconditions.DEPLOYED_PREFIX` is `deployed:` and
`preconditions.deployed_target(ref)` returns what follows it, `""` for every other ref.
`checked_ref` canonicalises `deployed: wo-x` to `deployed:wo-x` and refuses an empty target
— a hold nothing names can never clear.

A `wo-` target, not only a sha, because the hold is filed BEFORE the dependency merges: at
filing time there is no commit to name. The OS resolves the id to the commit later, which
is what §2.2 does.

`--depends-on` is untouched. Nothing derives this hold from prose or from an edge: §2.3 of
the 2026-10-15 spec stands — the only writer is the explicit flag.

### 2.2 The OS clears it, every reconcile tick

`Daemon.clear_settled_preconditions` gains an arm before the `bl-` one, and two helpers:

- `clear_deployed_precondition(project, store, wo, row)` — guarded on
  `project.name == self._os_project()`, for the reason `_already_live` already states:
  `release.overtaken_by` reads `jarvis-*` tags and the OS's own production checkout, so it
  means nothing anywhere else.
- `deployed_hold_sha(project, store, target)` — a `wo-` target goes through the existing
  `_merge_commit_of` (the `pr_merged` / `pr_merge_commit_recorded` event, else one
  `gh pr view`); anything else IS the sha.

The row clears with `by="os"` when `release.overtaken_by(project.path, [sha])` reports a
containing tag AND `live`, i.e. the production checkout is detached at one of those tags
with its `pyproject.toml` version agreeing. `overtaken_by` is reused rather than reimplemented:
it already fetches tags, intersects `--contains` per sha, and compares git and the file.

Nothing clears on any of: a target work order that does not exist, one that has not merged
(no sha yet), a sha git does not know, a production checkout not at a tag. All of those
leave the row OPEN — the same posture §2.6 takes for a `bl-` ref that resolves to nothing:
clearing on absence would release an order whose hold was never satisfied.

Position is inherited, not chosen: the arm lives inside the pass `tick()` already calls
immediately before `dispatch_pending`, so a hold cleared on this tick is claimed on it.

### 2.3 `jarvis wo unblock`

`ops._split_preconditions` takes the store and classifies a `deployed:` ref as **live** when
the daemon can still resolve it — a non-`wo-` target always, a `wo-` target that exists in
this store — and **stranding** when the target names no work order. So the default
`jarvis wo unblock` cuts a typo'd target and refuses a real one, naming `--all`, exactly as
it already does for an unsettled `bl-` item.

### 2.4 Rejected alternatives

- **Making `depends_on` deployment-aware, by flag or by default.** It would change dispatch
  for every edge in the fleet to fix the orders whose subject is live fleet behaviour, and
  a project with no release path would have a dependency that never clears.
- **A `deploy_waits` table.** A precondition row already is the hold, is already excluded
  by the `precondition` `_ClaimRule`, already surfaces through `precondition_note`, and is
  already cut by `jarvis wo unblock`. A second mechanism would need all four again.
- **Answering the OS-project question in `unblock_work_order` too.** It has no catalog, and
  loading one to turn one message's word from "live" to "stranded" on a hold mis-filed
  against a non-OS project is not worth a catalog read on every unblock. The asymmetry is
  that such a hold needs `--all`; `--all` works.
- **Deriving the hold from the brief.** §2.4 of the 2026-10-15 spec: prose holds are
  detected, never attached.

## 3. Test obligations

`tests/test_preconditions.py`, under a `§ deployed:` heading:

1. `checked_ref` canonicalises the target and refuses `deployed:` alone.
2. The flag writes the row through `ops.create_work_order` and the order is not claimed.
3. A `deployed:wo-X` hold does NOT clear while `wo-X` has no merge commit.
4. It does NOT clear when a tag contains the commit but production is on an earlier tag.
5. It DOES clear, `by="os"`, once production is at a tag containing the commit, and the
   order is claimable on the same pass.
6. A literal `deployed:<sha>` target clears on the same condition.
7. A `deployed:wo-nosuch` hold stays open and `jarvis wo unblock` cuts it without `--all`;
   a resolvable one is refused and `--all` is named.

## 4. Out of scope

Telling the filer, at creation, that a `deployed:` hold on a non-OS project will never be
derived. Nothing checks ref existence at creation (§2.3 of the 2026-10-15 spec) and this
would be the first such check.
