# A red `main` dispatches its own fix

GitHub issue 1055. Work order wo-a577c9fd.

## The problem

The OS detects a red default branch completely and acts on it in every direction except
the one that fixes it.

What already works:

- `Daemon.poll_default_branch` (`src/jarvis/daemon.py:6899-6993`) reads `gh run list` once
  per project per tick and writes `base_health:<project>` (`central_store.py:2271-2293`).
- `invariants.check_default_branch_green` (`src/jarvis/invariants.py:5179-5226`) raises
  `INV-BASE-BRANCH-RED` off that fact, deduped by `open_violation_report`.
- `automerge.decide`'s condition 7 (`src/jarvis/automerge.py:362-377`) holds every merge
  in the fleet on `HELD_BASE_RED`.

What nothing does: dispatch the fix. Evidence, 2026-10-14:

- GitHub issue 1054 (red `main`, filed by a worker) sat OPEN and unassigned.
- wo-024e207b was held `base_red` for 62 minutes with a round already `passed` — a merge
  blocked by a break no order was working on.
- The inbox's only remedy was prose: the user types `jarvis issues start`
  (`issues.START_COMMAND`). `check_default_branch_green`'s detail says only "The remedy is
  a commit on `main`", naming nobody.
- Duplicate filings 1007/1010 for one break cost ~19 USD, so "file every time the base is
  red" is not the fix either.

Root cause, stated plainly: the red-base feature was specified as a DETECTOR plus a BRAKE
(spec `docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md` §1-§4).
There is no actor. A red `main` is the one defect where the OS is the obligated party —
the break is usually the OS's own merge (`Daemon._attribute_base_red`,
`daemon.py:6995-7028`, fills `wo_id`), every parked order inherits it, and no user decision
is involved. Making the user the actor is the defect, not a UI shortfall.

Not the root cause and not in scope: why `main` went red in the first place (that is the
dispatched order's job), and other projects' red branches (see §Scope).

## The fix

A per-tick, idempotent sweep that files-or-adopts a tracker issue and dispatches a work
order on it, scoped to the OS's own tracker project, plus a hold reason that names the
order doing it.

### 1. Scope: the tracker project only

Neo, question 1736. Act only for the project whose git `origin` IS `bugreport.bug_repo()`,
resolved exactly as `issues.tracker_project(catalog, repo)` does
(`src/jarvis/issues.py:909-929`). Every other project stays notify-only, byte-identical to
today: detector, invariant, brake, no dispatch.

Not `Daemon._os_owner()` (`daemon.py:5572-5576`): that answers "who runs fleet-wide
checks", which is a different question from "whose repository is the bug tracker". Using it
would act for a project that owns no tracker.

### 2. New daemon method `Daemon.dispatch_base_fix(project, store)`

Called from `tick` BETWEEN `poll_default_branch` and `poll_pull_requests`
(`daemon.py:1282-1283`), on the existing `poll_prs` cadence:

- after the poll, so the fact it reads is this tick's reading and not the previous
  interval's — the same reason the poll itself sits before the PR poll;
- before `poll_pull_requests`, so the `fix_wo_id` it writes is visible to the SAME tick's
  `auto_merge` calls (`daemon.py:7274`, `7294`, `7332`) and the hold sentence names the
  order from the tick it was dispatched, not one interval later.

A SWEEP, NOT A TRANSITION ACTION. `poll_default_branch`'s announcements fire on transitions
because an announcement must not repeat; a dispatch must be RETRIED, because every way it
fails is transient and outside the OS: Neo unreachable, project in the catalog but not
registered, `gh` down, `promote_confirmed` raising. A transition-only dispatch would lose
the break for as long as it stayed red. Idempotence comes from the fact's own keys (§3), so
re-running it on a tick where nothing changed costs zero `gh` calls.

ONE FIX ORDER PER RED-BASE ISSUE, EVER. Once `fix_wo_id` is set it is NEVER cleared while
the base stays red — not on `failed`, not on `cancelled`, not on `completed`. The sweep's
retry is about getting ONE order to exist, not about replacing one that does. Read the two
cases together, because they look alike and are not:

- NO order exists yet (`fix_wo_id` empty). Every failure mode above left nothing behind, so
  the next tick's attempt is the FIRST dispatch of that break, not a second one. Retrying
  here is the whole point of being a sweep.
- An order EXISTS and has reached a terminal status with the base still red. The OS is out
  of moves: it dispatches nothing more and it does not auto-retry, because `jarvis wo retry`
  is user-only by contract — a turn that died with no result is replayed by the user, never
  by the OS. It raises one attention item instead (step 5).

Body, in order; every step is a no-op if the one before already succeeded:

1. Scope gate (§1); return otherwise.
2. `self._base_red(project)` is None → return. Freshness is judged in exactly one place
   (`BASE_HEALTH_FRESH_SECONDS`, `daemon.py:7030-7056`); a stale fact dispatches nothing
   for the same reason it pauses no merge.
3. Read the raw fact for `run_id`, `run_url`, `issue_url`, `fix_wo_id`.
4. `fix_wo_id` names an order whose status is NOT in `project_store.TERMINAL_STATUSES`
   (`project_store.py:69` — `completed`, `cancelled`, `failed`) → return. Someone is on it.
5. `fix_wo_id` names a TERMINAL order and the base is still red → that order did not fix
   it, and the OS has nothing left to try. Dispatch NOTHING. Keep BOTH keys exactly as they
   are: no clear, no second order. The user is told once, through
   `check_default_branch_green`'s third remedy branch (§5), which names that order and its
   terminal status and offers `jarvis wo retry` or a different order. Then return.
6. `issue_url` set → `issues.start_work(issue_url, project=project.name)`
   (`issues.py:1524-1570`), which is idempotent through `promote_confirmed` /
   `live_work_order` (`issues.py:1407-1424`): one issue, one live work order. Record the
   returned `wo_id`.

   UNLESS THAT STORED ISSUE IS NOW CLOSED, in which case CLEAR `issue_url`, log it and
   return: the next tick adopts-or-files afresh. `start_work` refuses a closed issue
   outright (`issues.py:1563-1565`), so without this the step raises on every tick for ever
   — no work order, no progress, no attention item, the base red indefinitely. Which is
   this spec's own defect one level down, and the reason it is NOT left to step 9's retry:
   a failure that cannot succeed is not transient. `fix_wo_id` is necessarily EMPTY when
   this step runs (steps 4 and 5 return on any value), so there is nothing to clear
   alongside it and the one-order rule is untouched. Dedupe survives the clear because §4's
   OPEN filter can never adopt that issue back. The state is READ (`issues.view`) rather
   than inferred from the refusal, so the never-succeeds case stays distinguishable from
   the transient ones — `gh` down, Neo unreachable, project unregistered — which must KEEP
   `issue_url` and retry.
7. No `issue_url` → ADOPTION SEARCH (§4). A hit: record its url and go to step 6.
8. Nothing found → FILE: `bugreport.report_bug(title=…, description=…, expected=…,
   actual=…, priority="high", expedite=True)` (`bugreport.py:394-442`). That one call
   already yields the issue, the `priority:` label, Neo's triage and — through
   `issues.route_filing`'s expedite branch (`issues.py:1049-1070`) — the work order.
   Record `url` and `pickup.wo_id`.
9. Every failure: `log.exception`, write back whatever DID succeed (an issue filed with no
   work order still stores `issue_url`), return. Next tick retries from the first step that
   is still empty. That retry can only ever produce the FIRST order for the break: once
   `fix_wo_id` holds an id, step 4 or step 5 returns before any dispatch.

`priority="high"` and NOT `critical`/`blocker`: `expedite=True` is what dispatches, at any
priority (`route_filing`'s docstring), and the rating must stay an honest description. A
claimed `critical` would also hand Neo a re-assessment that decides nothing here, since the
work is dispatched either way.

The description must carry `run_url`, `workflow`, `head_sha` and the attributed `wo_id`
verbatim — `run_url` is also what the next sweep's adoption search keys on (§4).

### 3. Two new keys on the base-health fact

`issue_url` and `fix_wo_id`, written by `dispatch_base_fix` and carried by the poll:

- `poll_default_branch`'s red-CONTINUES branch (`daemon.py:6967-6970`) carries both forward
  out of `prev` exactly as it already carries `wo_id`;
- the fact literal (`daemon.py:6971-6981`) sets them from those locals, so the GREEN
  transition simply does not set them and both keys vanish with the red fact. No explicit
  clear, no second code path.

DEDUPE ON THE ISSUE, NOT ON THE RUN. A second red run on the same break replaces `run_id`
and `run_url` in the fact but inherits `issue_url`/`fix_wo_id`, so it joins the existing
order instead of filing again. Keying on the run id would file once per red run — that is
the 1007/1010 shape.

### 4. The adoption search and its exact filter

`issues.issues_mentioning(repo, term)` (`issues.py:556-580`) with `term = str(run_id)`.

Mechanical, not fuzzy: a worker-filed red-main issue quotes the failing run's URL in its
body, and the run id is the one token in it that is unique to this break (it is in 1054).
`gh` search is token-AND, so searching the whole URL buys nothing the id does not and
misses a body that wrote the id without the URL.

TWO FILTERS, both required:

1. `state == "OPEN"`. `issues_mentioning` deliberately searches `--state all` — that
   keyword is load-bearing for its own callers, where a closed duplicate dropping out
   means re-filing for ever. Here the opposite holds: a CLOSED issue citing this run is a
   fix that was tried and did not work, and adopting it would reopen nothing and dispatch
   against a closed issue (`start_work` refuses a closed issue outright,
   `issues.py:1563-1565`).
2. Lowest issue number among the OPEN hits — the first one filed, so two concurrent
   filings converge on one.

Failing direction: an issue about this break that never quoted the run id gets a duplicate
filing. That is the right way to fail — a duplicate issue is visible and closable; the same
asymmetry `ops._verdict_duplicate` (`ops.py:10067-10107`) argues for.

### 5. The hold reason names the order

- `automerge.BaseRed` (`automerge.py:189-204`): new field `fix_wo_id: str = ""`.
- `Daemon._base_red` (`daemon.py:7030-7056`): fills it from the fact, beside `wo_id`.
- `automerge.decide` condition 7 (`automerge.py:362-377`): `HELD_BASE_RED`'s reason gains
  "`<fix_wo_id>` is fixing it" when set, and keeps today's "Nothing merges onto a broken
  default branch." only as the tail. Empty `fix_wo_id` must read as nothing-yet rather than
  as silence.
- `invariants.check_default_branch_green` (`invariants.py:5179-5226`): the detail replaces
  "The remedy is a commit on `main`" with the order that is producing that commit; `context`
  gains `fix_wo_id` and `issue_url`.

THREE remedy branches, not two, because the terminal case is the one the user has to
decide. `fix_wo_id` empty: today's "no work order is producing it yet". `fix_wo_id` live:
that order is producing it. `fix_wo_id` TERMINAL (`store.get_work_order(fix_wo_id)` status
in `TERMINAL_STATUSES`) — the §2 step 5 case — exactly this:

> The remedy is a commit on `main`; wo-xxxxxxxx was dispatched to produce it and is now
> `failed`, so the OS is out of moves — it dispatches no second fix order and never
> retries a dead turn itself. Run `jarvis wo retry wo-xxxxxxxx`, or order a different fix.

DERIVED, never stored: the status is read back at render time, under the same rule as the
rest of this invariant. One attention item, because `INV-BASE-BRANCH-RED` is already
deduped by `open_violation_report` on the sentence.

TICK-STABLE, which is the constraint from the kn-fb4aa3c4 trap (1): a blocker or attention
sentence may carry NO value that changes tick to tick, because the dedupe key is the
sentence — `ack_attention` stores the derived blockers verbatim and `true_blockers`
compares by exact string equality. The three values in the terminal sentence (base,
`fix_wo_id`, the terminal status) are all frozen for as long as the base stays red:
`fix_wo_id` is written once per break and never rewritten (§2), and a terminal status is
terminal. No elapsed time, no run count, no "checked at", no retry counter goes into any of
these sentences.

### 6. The fix order is exempt from its own hold

`decide` skips condition 7 entirely when the work order being judged IS
`base_red.fix_wo_id` (`wo.get("id") == base_red.fix_wo_id`). Without this the mechanism
deadlocks: the order that repairs `main` is parked `waiting_pr_merge`, its own merge is
held on the base being red, and only its merge turns the base green.

Safe because the exemption is narrow and evidence-backed: the fix PR's CI runs on the merge
ref, so a correct fix is GREEN there, and conditions 4-6 (round passed, head unmoved,
GitHub green and CLEAN) are untouched. A broken fix fails condition 6 and never merges.

In `decide` and not in the daemon: condition 7's whole value is that the table is pure and
unit-testable without a network (`decide`'s docstring), and an exemption applied by
blanking `base_red` at the call site would make the holds of three other callers
(`daemon.py:7274`, `7294`, `7332`) depend on which branch reached them.

### 7. Rejected alternatives

- **Fire on the red transition only** — the obvious shape, matching the existing
  announcements. Loses: a dispatch has four transient failure modes outside the OS, and a
  spent transition leaves the break unowned for as long as it stays red. That is the
  original defect with a smaller window.
- **Re-dispatch a second fix order when the first one dies** — what this spec said until
  the user ruled on assumption #2 (Neo question 1738); clear `fix_wo_id` on a terminal
  order, keep the issue, let the next tick dispatch again. Loses twice. (a) UNCAPPED SPEND:
  nothing bounds the number of orders, so a break no order can fix buys a new worker every
  time one dies, for as long as `main` stays red. (b) THE SAME CAUSE RECURS: every reason
  offered for a second order — Neo unreachable, `gh` down, project not registered — is
  environmental and kills the replacement exactly as it killed the first. A replacement
  order is only ever a fresh attempt at a failure that was never about the order. The
  retry that IS kept is the different one: retrying until ONE order exists (§2).
- **Auto-retry the dead fix order instead of raising attention** — cheaper than a new
  order, same session. Loses: `jarvis wo retry` is user-only by contract — "a turn that
  died with no result is never replayed by the OS, only by the user" — and a daemon that
  replayed it would re-send a whole dead conversation on a loop, which is the spend
  objection again in a thinner disguise.
- **Dedupe on `run_id`** — simplest key. Loses: one break produces many red runs; this is
  exactly how 1007/1010 happened.
- **Dispatch for every project** — looks more useful. Loses: another project's red branch
  may be a human's push, its tracker is not the OS's, and `bug report` files to the OS's
  repository. Neo already ruled (1736).
- **`io create` / an improvement order instead of a work order** — the root-cause route.
  Loses: an improvement order reports findings and writes no code, so `main` stays red.
- **Hand the exemption to `Daemon.auto_merge`** — fewer `decide` changes. Loses: purity and
  one rule in one place; see §6.
- **Tell the user better** — a sharper inbox line with the exact command. Loses: it is the
  same ask, and the user's standing rule is that a gap the OS notices is one the OS closes
  itself. (The terminal-order attention item above is NOT this: it fires only once the OS
  has spent every move it has.)

### 8. Tests a reviewer should expect

In `tests/test_base_red.py`, reusing its `poll`/`fact`/`spec`/`red_fleet` helpers.

Now (must all exist):

1. A non-tracker project with a red base dispatches NOTHING — no `gh issue` call, no work
   order, and the fact gains neither key.
2. A second red run while `issue_url`/`fix_wo_id` are set files no issue and creates no
   second order; the fact's `run_id` moves and both keys survive.
3. An OPEN issue whose body quotes the run id is ADOPTED — `start_work` on that url, and
   `report_bug` is never called.
4. A CLOSED issue quoting the same run id is NOT adopted: a fresh issue is filed.
5. A dispatch that fails (`report_bug` raising; Neo unreachable; project not registered)
   leaves the fact without `fix_wo_id` and the NEXT tick retries and succeeds. This is the
   retry the one-order rule KEEPS: no order existed, so the second attempt is the first
   dispatch.
6. The fix order itself is NOT held on `base_red`: `decide` arms for
   `wo["id"] == base_red.fix_wo_id` with everything else green, and still holds for any
   other order on the same fact.
7. The green transition drops BOTH keys; a later re-break starts from an empty fact and
   dispatches again.
8. `fix_wo_id` pointing at a TERMINAL order while the base is still red dispatches NOTHING
   — no `report_bug`, no `start_work`, `fix_wo_id` unchanged — and `INV-BASE-BRANCH-RED`'s
   detail names that order and its terminal status. Parametrised over all three of
   `completed`, `cancelled`, `failed`: the rule is the same for each, and `completed` is
   the one a reader expects to behave differently.
9. That terminal-case detail is BYTE-IDENTICAL on two successive ticks with the fact
   re-polled in between (red continues, `run_id` held constant), so the violation report is
   announced once and `ack_attention` can match it.
10. `HELD_BASE_RED`'s reason and `INV-BASE-BRANCH-RED`'s detail contain the work-order id
    and are byte-identical across three consecutive reconcile passes (the kn-fb4aa3c4
    tick-stability property, so the violation report is still announced once).
11. A stale fact (past `BASE_HEALTH_FRESH_SECONDS`) dispatches nothing.
12. A STORED `issue_url` whose issue has since been CLOSED, on a still-red base, clears
    `issue_url` rather than raising, and the next tick files a new issue (§2 step 6). The
    retry loop it closes produces no work order at all, so a test that only drives the
    open-issue path cannot see it.

### 9. Deliberately NOT in scope

- No new CLI verb and no dashboard control. The fix order appears on the usual surfaces;
  `jarvis wo cancel` is the override.
- No change to release gating, `hold_red_release` or `sync_issues`.
- No change to `_attribute_base_red`: blaming the break stays as it is, and `wo_id` remains
  independent of `fix_wo_id`.
- No automatic revert of the breaking merge. The dispatched worker decides between revert
  and forward fix.
- Other projects remain notify-only.
- No OS-side retry of a dead fix order, and no second fix order per break (§7). The user
  drives `jarvis wo retry` from the attention item.

### 10. Decisions and open questions

SETTLED by the user on recorded assumption #2, 2026-10-14, agreeing with Neo's escalation
(question 1738): one fix work order per red-base issue, ever. The user's words — "don't
re-dispatch a fix order every time, just do it once and the OS has to make sure that one
goes through; all reasons listed to justify a second one (Neo unreachable, gh down, project
unregistered) will also fail with new orders". §2's one-order rule, §5's third remedy
branch, §7's rejected alternative and §8 tests 8-9 are that ruling. The re-dispatch step
this spec carried before is removed, not deferred.

STILL OPEN for the reviewer: whether `priority="high"` is right, or whether a red tracker
`main` should claim `critical` and let Neo re-assess. The dispatch does not depend on it
(`expedite` does that work), so this only changes the `priority:` label and whether a
release is cut on landing.
