# A red `main` dispatches its own fix

GitHub issue 1055. Work order wo-a577c9fd.

## The problem

The OS detects a red default branch completely and acts on it in every direction except
the one that fixes it.

What already works:

- `Daemon.poll_default_branch` (`src/jarvis/daemon.py:6899-6993`) reads `gh run list` once
  per project per tick and writes `base_health:<project>` (`central_store.py:2271-2293`).
- `invariants.check_default_branch_green` (`src/jarvis/invariants.py:5186-5267`) raises
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

Second problem, found in review of this branch's first implementation and fixed here: the
adoption search of §4 trusted the tracker. The tracker repository is PUBLIC and the failing
run id is PUBLIC, so a stranger who reads a red run can open an issue quoting that run id,
win the lowest-number race inside one tick, and have their own issue body become the brief
of a worker the OS starts itself, in the OS's own repository, with no human in the loop.
That is a remote prompt-injection-to-dispatch path, not a nuisance. §4 and §6 below are
re-specified against it.

Not the root cause and not in scope: why `main` went red in the first place (that is the
dispatched order's job), and other projects' red branches (see §Scope).

## The fix

A per-tick, idempotent sweep that files-or-adopts a tracker issue the OS ITSELF filed and
dispatches a work order on it, scoped to the OS's own tracker project, plus a hold reason
that names the order doing it.

### 1. Scope: the tracker project only

Neo, question 1736. Act only for the project whose git `origin` IS `bugreport.bug_repo()`,
resolved exactly as `issues.tracker_project(catalog, repo)` does
(`src/jarvis/issues.py:909-929`). Every other project stays notify-only, byte-identical to
today: detector, invariant, brake, no dispatch.

Not `Daemon._os_owner()` (`daemon.py:5572-5576`): that answers "who runs fleet-wide
checks", which is a different question from "whose repository is the bug tracker". Using it
would act for a project that owns no tracker.

### 2. `Daemon.dispatch_base_fix(project, store)`

`daemon.py:7007-7086`. Called from `tick` BETWEEN `poll_default_branch` and
`poll_pull_requests` (`daemon.py:1282-1283`), on the existing `poll_prs` cadence:

- after the poll, so the fact it reads is this tick's reading and not the previous
  interval's — the same reason the poll itself sits before the PR poll;
- before `poll_pull_requests`, so the keys it writes are visible to the SAME tick's
  `auto_merge` calls (`daemon.py:7274`, `7294`, `7332`) and the hold sentence names the
  order from the tick it was dispatched, not one interval later.

A SWEEP, NOT A TRANSITION ACTION. `poll_default_branch`'s announcements fire on transitions
because an announcement must not repeat; a dispatch must be RETRIED, because every way it
fails is transient and outside the OS: Neo unreachable, project in the catalog but not
registered, `gh` down, `promote_confirmed` raising. A transition-only dispatch would lose
the break for as long as it stayed red. Idempotence comes from the fact's own keys (§3), so
re-running it on a tick where nothing changed costs zero `gh` calls.

ONE FIX ORDER PER BREAK, EVER. This is the user's ruling (§10), and it is unconditional:
once `fix_wo_id` is set the sweep returns on every later tick, whatever became of that
order — `completed`, `cancelled`, `failed`, or its row deleted. There is no status read in
the sweep at all, and no second dispatch anywhere in this feature. A dead fix order is
reported (§5) and never replaced.

Body, in order; every step is a no-op if the one before already succeeded:

1. Scope gate (§1); return otherwise.
2. `self._base_red(project)` is None → return. Freshness is judged in exactly one place
   (`BASE_HEALTH_FRESH_SECONDS`, `daemon.py:7030-7056`); a stale fact dispatches nothing
   for the same reason it pauses no merge.
3. Read the raw fact for `run_id`, `issue_url`, `fix_wo_id`.
4. `fix_wo_id` NON-EMPTY → return, with no `gh` call and no status lookup. The one-order
   rule, in one line.
5. `issue_url` set and `issues.view(issue_url, repo).closed` → CLEAR `issue_url`, log, and
   return without dispatching; the next tick starts from step 6 with an empty url.
   `start_work` refuses a closed issue outright (`issues.py:1563-1565`), so without this
   the step raises on every tick for ever — no work order, no progress, no attention item,
   the base red indefinitely. A failure that can never succeed is not transient, which is
   why it is NOT left to step 9's retry. `fix_wo_id` is necessarily empty here (step 4
   returned on any value), so nothing is cleared beside the url and the one-order rule is
   untouched: this clears a filing that produced NO order, not an order. §4's filter can
   never adopt that closed issue back. The state is READ rather than inferred from the
   refusal, so the never-succeeds case stays distinguishable from the transient ones —
   `gh` down, Neo unreachable, project unregistered — which must KEEP `issue_url` and
   retry.
6. No `issue_url` → ADOPTION SEARCH (§4). A hit is an issue the OS itself filed; record its
   url.
7. An `issue_url` in hand (stored, or just adopted) → `issues.start_work(issue_url,
   project=project.name)` (`issues.py:1524-1570`), idempotent through `promote_confirmed` /
   `live_work_order` (`issues.py:1407-1424`): one issue, one live work order. Record the
   returned `wo_id`.
8. Nothing adopted → FILE: `bugreport.report_bug(title=…, description=…, expected=…,
   actual=…, priority="high", expedite=True, project=…)` (`bugreport.py:394-442`). That one
   call already yields the issue, the `priority:` label, Neo's triage and — through
   `issues.route_filing`'s expedite branch (`issues.py:1049-1070`) — the work order. Record
   `url` and `pickup.wo_id`.
9. Every failure: `log.exception`, write back whatever DID succeed (an issue filed with no
   work order still stores `issue_url`), return. Next tick retries from the first step that
   is still empty. That retry can only ever produce the FIRST order for the break: once
   `fix_wo_id` holds an id, step 4 returns before any dispatch.

`priority="high"` and NOT `critical`/`blocker`: `expedite=True` is what dispatches, at any
priority (`route_filing`'s docstring), and the rating must stay an honest description. A
claimed `critical` would also hand Neo a re-assessment that decides nothing here, since the
work is dispatched either way.

The description (`Daemon._base_red_brief`) must carry `run_url`, `workflow`, `head_sha` and
the attributed `wo_id` verbatim — `run_url` is also what the next sweep's adoption search
keys on (§4).

### 3. Three new keys on the base-health fact

`issue_url`, `fix_wo_id` and `issue_os_filed` (boolean), written by `dispatch_base_fix` and
carried by the poll:

- `poll_default_branch`'s red-CONTINUES branch (`daemon.py:6967-6970`) carries all three
  forward out of `prev` exactly as it already carries `wo_id`;
- the fact literal (`daemon.py:6971-6981`) sets them from those locals, so the GREEN
  transition simply does not set them and all three vanish with the red fact. No explicit
  clear, no second code path.

`issue_os_filed` is True only for an issue that passed §4's author-plus-marker test, or
that `report_bug` filed in this sweep. It exists so `automerge.decide` can apply §6's
exemption with NO network call: `decide` is a pure table (its docstring), and asking `gh`
who authored an issue from inside it would put a subprocess behind every merge decision.

DEDUPE ON THE ISSUE, NOT ON THE RUN. A second red run on the same break replaces `run_id`
and `run_url` in the fact but inherits all three keys, so it joins the existing order
instead of filing again. Keying on the run id would file once per red run — that is the
1007/1010 shape.

### 4. The adoption search and its exact filter

`issues.issues_mentioning(repo, term)` (`issues.py:556-581`) with `term = str(run_id)`,
then four filters. All four are required; dropping any one of them either re-files or
adopts an attacker's issue.

Mechanical, not fuzzy: a red-main issue the OS filed quotes the failing run's URL in its
body, and the run id is the one token in it that is unique to this break (it is in 1054).
`gh` search is token-AND, so searching the whole URL buys nothing the id does not and
misses a body that wrote the id without the URL.

1. `state == "OPEN"`. `issues_mentioning` deliberately searches `--state all` — that
   keyword is load-bearing for its own callers, where a closed duplicate dropping out
   means re-filing for ever. Here the opposite holds: a CLOSED issue citing this run is a
   fix that was tried and did not work, and `start_work` refuses a closed issue outright.
2. THE AUTHOR IS THE OS. The issue's author login must equal the login of the
   `gh`-authenticated account the OS files as (`gh api user --jq .login`, read once and
   cached for the process — it cannot change under a running daemon, and the search is
   already one `gh` call).
3. THE BODY CARRIES THE FILING MARKER, verbatim: `<!-- Filed automatically by `jarvis bug
   report`. -->` (`bugreport.py:278`).
4. Lowest issue number among the hits that passed 1-3 — the first one the OS filed, so two
   concurrent filings converge on one.

**BOTH 2 AND 3, never either.** They fail in opposite directions and only the pair is
sound. The marker is public text present in every filed body, so a stranger can copy it;
the author login is what a stranger cannot forge. The marker alone admits a forged issue;
the author alone would adopt any issue the OS's account ever opened for another reason that
happens to quote the id.

THE ATTACK THIS CLOSES. The tracker is public and so is the run id. With the run id as the
only key, a stranger reads a red run, opens an issue quoting it, and — issue numbers being
monotonic, so a new issue loses the lowest-number race only to an issue that already exists
— wins that race within one tick whenever the OS has not filed yet. `issues.start_work`
then dispatches a worker in the OS's own repository whose brief is that body. Remote text
becomes an instruction to a privileged agent with no human in the loop. The filter makes
adoption a check that the OS is recognising its OWN filing, which is the only thing
adoption was ever for: it exists to avoid duplicate filings by the OS, not to pick up other
people's reports.

Failing direction: anything that does not pass all four filters falls through to a FRESH
filing (step 8). A duplicate issue is visible and closable; the same asymmetry
`ops._verdict_duplicate` (`ops.py:10067-10107`) argues for. A stranger's issue about the
same break is simply ignored by the sweep — a human can still pick it up with
`jarvis issues start`, which is a human in the loop and therefore a different thing.

`issues_mentioning`'s `--json` list currently selects `number,title,url,state`; the author
and body it must now return are added there, leaving its other callers' fields intact.

### 5. The hold reason names the order

- `automerge.BaseRed` (`automerge.py:189-208`): fields `fix_wo_id: str = ""`,
  `issue_url: str = ""`, `issue_os_filed: bool = False`.
- `Daemon._base_red` (`daemon.py:7030-7056`): fills them from the fact, beside `wo_id`.
- `automerge.decide` condition 7 (`automerge.py:362-377`): `HELD_BASE_RED`'s reason gains
  "`<fix_wo_id>` is fixing it" when set, and keeps "Nothing merges onto a broken default
  branch." as the tail. Empty `fix_wo_id` reads "no work order is fixing it yet" — nothing
  yet, never silence.
- `invariants.check_default_branch_green` (`invariants.py:5186-5267`): the detail replaces
  "The remedy is a commit on `main`" with the order that is producing that commit; `context`
  gains `fix_wo_id` and `issue_url`.

THREE remedy branches in the detail, because the dead case is the one the user has to
decide. `fix_wo_id` empty: "The remedy is a commit on `main`, and no work order is
producing it yet." `fix_wo_id` live: "…, and wo-xxxxxxxx is producing it." `fix_wo_id`
DEAD — `_dead_base_fix` (`invariants.py:5291-5306`) returns the order's status when it is
in `project_store.TERMINAL_STATUSES`, or the literal `deleted` when the row is gone:

> The remedy is a commit on `main`; wo-xxxxxxxx was dispatched to produce it and is now
> `failed`, so the OS is out of moves — it dispatches no second fix order and never
> retries a dead turn itself. <tail>

TWO VIOLATIONS IN THE DEAD CASE, and that is deliberate. `INV-BASE-BRANCH-RED` still
carries the detail above — it STATES the state, as the red-base finding always has — and a
second violation `INV-BASE-FIX-DEAD` (`invariants.py:265`) carries the DECISION, with
`owed=` set to its own detail and `wo_id=fix_wo_id` because `owed_violations` selects by
that field:

> wo-xxxxxxxx, the work order fixing the red default branch `main`, settled `failed`
> without clearing it, and the OS will not open another. <tail>

One would not do. `INV-BASE-BRANCH-RED` is not an owed finding — it clears itself when
somebody pushes a green commit, by anyone, with no decision taken — and putting `owed=` on
it would make the red branch itself a debt the user has to answer. The debt is the narrower
fact that the OS has spent its last move, which is exactly `INV-BASE-FIX-DEAD`'s subject
and exactly what must survive on the owed list after the red fact is re-polled.

THE TAIL BRANCHES ON THE STATUS, because `jarvis wo retry` is `failed`-ONLY by contract: it
refuses a `completed` or `cancelled` order, and a deleted row has no session to resume.
Naming it there would print a command that cannot run. `_base_fix_decision`
(`invariants.py:5270-5288`) is the single source of all three tails, used by both
violations so they can never drift:

- `failed` → ``Run `jarvis wo retry wo-xxxxxxxx`, or order a different fix.``
- `completed` / `cancelled` → ``That order will not run again — a fix is a new order on the
  same issue: `jarvis issues start <issue_url>`.``
- row GONE → ``Its record is gone, so nothing can be resumed — a fix is a new order on the
  same issue: `jarvis issues start <issue_url>`.``

The last two fall back to `… — order a fix yourself.`, naming NO command, when the fact
holds no `issue_url`: an `issues start` with nothing to start on is worse than prose.

DERIVED, never stored: the status is read back at render time, under the same rule as the
rest of this invariant.

TICK-STABLE, which is the constraint from the kn-fb4aa3c4 trap (1): a blocker or attention
sentence may carry NO value that changes tick to tick, because the dedupe key is the
sentence — `ack_attention` stores the derived blockers verbatim and `true_blockers`
compares by exact string equality. Every value in these sentences (base, `fix_wo_id`, the
terminal status, `issue_url`) is frozen for as long as the base stays red: `fix_wo_id` is
written once per break and never rewritten (§2), and a terminal status is terminal. No
elapsed time, no run count, no "checked at", no retry counter goes into any of them.

### 6. The fix order's exemption from the base_red hold

Without an exemption the mechanism deadlocks: the order that repairs `main` is parked
`waiting_pr_merge`, its own merge is held on the base being red, and only its merge turns
the base green. `decide` therefore skips condition 7 for that one order — and the key is
WHAT THE REMEDY IS FOR, not the OS's dispatch bookkeeping. All three must hold:

1. `wo["issue_url"]` is non-empty, AND
2. `base_red.issue_url` is non-empty, AND they are equal, AND
3. `base_red.issue_os_filed` is True.

`fix_wo_id` alone exempts NOTHING any more. Standing instruction kn-af41186c: an exemption
from a fleet-wide hold is keyed on what the remedy is FOR, never on the OS's own dispatch
bookkeeping. `fix_wo_id` is bookkeeping — it says only "this is the id the sweep happened
to write" — whereas the issue identity says "this pull request is the repair of this
break", which is the fact that justifies merging onto a branch nobody else may merge onto.
Condition 3 is what makes the identity trustworthy, and it is read off the fact rather than
fetched so `decide` stays pure and network-free (§3).

THE `'' == ''` TRAP, which kn-af41186c names and which the non-emptiness halves exist for:
`project_store.create_work_order` stores `issue_url or None`, so an ordinary work order's
column is NULL, not `''`. Compare the two sides without requiring BOTH non-empty and a
falsy-equals-falsy match — `None`/`''`/missing against a fact that has not filed yet —
exempts EVERY order in the fleet from the red-base hold, silently, which is the whole brake
switched off. Both halves require a non-empty stored value; neither is a convenience check.

Narrow and evidence-backed: CONDITIONS 4-6 STILL RUN after the exemption — round passed,
head unmoved, GitHub green and CLEAN. The fix PR's CI runs on the merge ref, so a correct
fix is green there; a broken remedy fails its own checks and falls through to the
checks-failed hold rather than merging. The exemption removes one hold, not the gate.

In `decide` and not in the daemon: condition 7's whole value is that the table is pure and
unit-testable without a network, and an exemption applied by blanking `base_red` at the
call site would make the holds of three other callers (`daemon.py:7274`, `7294`, `7332`)
depend on which branch reached them.

### 7. Rejected alternatives

- **Adopt on the run id alone, checking only the issue's state** — the first implementation
  of this spec, and the smallest thing that dedupes. Loses on security, not on taste: the
  tracker and the run id are public, so a stranger quoting the run id wins the lowest-number
  race within a tick and their issue body becomes the brief of a worker the OS starts in
  its own repository with no human in the loop (§4). Rejected in review of this branch.
- **The filing marker alone, without the author check** — one less `gh` call. Loses: the
  marker is public text in every filed body, so it is copy-pasteable; it proves the shape
  of a filing, not its origin.
- **The author check alone, without the marker** — the unforgeable half. Loses: it adopts
  any issue the OS's own account ever opened that happens to quote the id, including ones
  filed by other OS paths for other purposes.
- **Key the exemption on `fix_wo_id`** — what this spec said first, and the obvious key.
  Loses: it is the OS's bookkeeping, not the remedy's identity (kn-af41186c, §6), and the
  bookkeeping is writable by the sweep on evidence the sweep itself may have been fooled
  into trusting. Issue identity plus OS-filed is the fact that bears the weight.
- **Fire on the red transition only** — matching the existing announcements. Loses: a
  dispatch has four transient failure modes outside the OS, and a spent transition leaves
  the break unowned for as long as it stays red. That is the original defect with a smaller
  window.
- **Re-dispatch a second fix order when the first one dies** — clear `fix_wo_id` on a
  terminal order, keep the issue, let the next tick dispatch again. Loses twice. (a)
  UNCAPPED SPEND: nothing bounds the number of orders, so a break no order can fix buys a
  new worker every time one dies, for as long as `main` stays red. (b) THE SAME CAUSE
  RECURS: every reason offered for a second order — Neo unreachable, `gh` down, project not
  registered — is environmental and kills the replacement exactly as it killed the first.
  Ruled out by the user (§10). The retry that IS kept is the different one: retrying until
  ONE order exists (§2).
- **Auto-retry the dead fix order instead of raising attention** — cheaper, same session.
  Loses: `jarvis wo retry` is user-only by contract — "a turn that died with no result is
  never replayed by the OS, only by the user" — and a daemon that replayed it would re-send
  a whole dead conversation on a loop, which is the spend objection in a thinner disguise.
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
  itself. (The dead-order attention item is NOT this: it fires only once the OS has spent
  every move it has.)

### 8. Tests a reviewer should expect

In `tests/test_base_red.py`, reusing its `poll`/`fact`/`spec`/`red_fleet` helpers.

Dispatch and dedupe:

1. A non-tracker project with a red base dispatches NOTHING — no `gh issue` call, no work
   order, and the fact gains none of the three keys.
2. A second red run while `issue_url`/`fix_wo_id` are set files no issue and creates no
   second order; the fact's `run_id` moves and all three keys survive.
3. An OPEN, OS-AUTHORED, marker-carrying issue whose body quotes the run id is ADOPTED —
   `start_work` on that url, `report_bug` never called, `issue_os_filed` True.
4. A CLOSED issue otherwise passing every filter is NOT adopted: a fresh issue is filed.
5. A dispatch that fails (`report_bug` raising; Neo unreachable; project not registered)
   leaves the fact without `fix_wo_id` and the NEXT tick retries and succeeds. This is the
   retry the one-order rule KEEPS: no order existed, so the second attempt is the first
   dispatch.
6. The green transition drops all three keys; a later re-break starts from an empty fact
   and dispatches again.
7. A stale fact (past `BASE_HEALTH_FRESH_SECONDS`) dispatches nothing.
8. A STORED `issue_url` whose issue has since been CLOSED, on a still-red base, clears
   `issue_url` rather than raising, and the next tick files a new issue (§2 step 5). The
   retry loop it closes produces no work order at all, so a test that only drives the
   open-issue path cannot see it.

Adoption security (§4) — each one asserts BOTH halves: not adopted, AND a fresh issue
filed, so a rejected candidate can never silently stall the sweep:

9. A FOREIGN-AUTHORED open issue quoting the run id, carrying the marker, with a LOWER
   number than the OS's own filing, is NOT adopted. This is the attack as reported.
10. An OS-AUTHORED open issue quoting the run id WITHOUT the marker is NOT adopted.
11. A foreign-authored issue with a lower number sitting beside an OS-filed, marker-carrying
    one: the OS-filed issue is adopted, lowest-number-among-the-eligible, not
    lowest-number-overall.

The exemption (§6):

12. The fix order is NOT held on `base_red`: `decide` arms when the order's `issue_url`
    equals the fact's and `issue_os_filed` is True, everything else green — and still holds
    for any other order on the same fact.
13. TWO EMPTY `issue_url`s DO NOT EXEMPT: an ordinary order whose column is `None` (as
    `create_work_order` writes it) against a fact with no `issue_url` is HELD. Also with
    `''` on either side, since both spellings reach `decide`.
14. Matching `issue_url`s with `issue_os_filed` False are HELD.
15. A BROKEN REMEDY is still held: the exempt order with a red check of its own, or an
    unmoved-head/not-passed round, is held on the checks-failed reason — conditions 4-6
    survive the exemption.

Sentences:

16. The dead-fix case dispatches NOTHING — no `report_bug`, no `start_work`, `fix_wo_id`
    unchanged — and raises BOTH `INV-BASE-BRANCH-RED` (detail naming the order and its
    status) and `INV-BASE-FIX-DEAD` (with `owed` set and `wo_id == fix_wo_id`). Parametrised
    over `completed`, `cancelled`, `failed` and a DELETED row: the dispatch rule is the same
    for each, and `completed` is the one a reader expects to behave differently.
17. The tail is the right one per status: `jarvis wo retry` ONLY for `failed`;
    `jarvis issues start <url>` for `completed`/`cancelled`/deleted; "order a fix yourself"
    with no command when the fact holds no `issue_url`. Assert the absence of
    `wo retry` in the non-`failed` tails — printing an unrunnable command is the defect.
18. Both details are BYTE-IDENTICAL on two successive ticks with the fact re-polled in
    between (red continues, `run_id` held constant), so each violation report is announced
    once and `ack_attention` can match it.
19. `HELD_BASE_RED`'s reason and `INV-BASE-BRANCH-RED`'s detail contain the work-order id
    and are byte-identical across three consecutive reconcile passes (the kn-fb4aa3c4
    tick-stability property).

### 9. Deliberately NOT in scope

- No new CLI verb and no dashboard control. The fix order appears on the usual surfaces;
  `jarvis wo cancel` is the override.
- No change to release gating, `hold_red_release` or `sync_issues`.
- No change to `_attribute_base_red`: blaming the break stays as it is, and `wo_id` remains
  independent of `fix_wo_id`.
- No automatic revert of the breaking merge. The dispatched worker decides between revert
  and forward fix.
- Other projects remain notify-only.
- No OS-side retry of a dead fix order, and NO SECOND FIX ORDER PER BREAK under any
  condition (§2, §7). The OS reports that it is out of moves — both violations of §5 — and
  the user takes the decision the tail names.
- No adoption of issues the OS did not file. A stranger's report of the same break stays on
  the tracker for a human to pick up with `jarvis issues start`; the sweep ignores it (§4).
- No reopening of a closed issue, and no `issue_os_filed` recomputation once stored: the
  fact dies with the red branch.

### 10. Decisions and open questions

SETTLED by the user on recorded assumption #2, 2026-10-14, agreeing with Neo's escalation
(question 1738): one fix work order per red-base issue, ever. The user's words — "don't
re-dispatch a fix order every time, just do it once and the OS has to make sure that one
goes through; all reasons listed to justify a second one (Neo unreachable, gh down, project
unregistered) will also fail with new orders". §2's one-order rule, §5's two violations,
§7's rejected alternative and §8 tests 16-18 are that ruling. The re-dispatch step this
spec carried before is removed, not deferred.

REVIEW OF THIS BRANCH, 2026-10-15. Three required changes, all implemented here and none
deferred:

1. The branch was REJECTED on the adoption hole: run-id-only adoption lets a public,
   foreign-authored issue become a worker's brief. Fixed by §4's author-plus-marker filter,
   §7's rejected alternative, and tests 9-11.
2. The exemption was re-keyed off `fix_wo_id` onto the remedy's issue identity plus
   `issue_os_filed`, per standing instruction kn-af41186c, with the `'' == ''` trap it names
   written into §6 and asserted by test 13.
3. Three NON-BLOCKING findings against the spec text: §5 and §8 described a single
   `jarvis wo retry` tail where the code branches on the status, §2 and §5 kept remnants of
   the pre-ruling re-dispatch remedy, and §2/§5/§9 did not describe the shipped two-violation
   dead-fix behaviour. All three are now written from the code
   (`invariants._base_fix_decision`, `_dead_base_fix`, `check_default_branch_green`,
   `Daemon.dispatch_base_fix`).

STILL OPEN for the reviewer: whether `priority="high"` is right, or whether a red tracker
`main` should claim `critical` and let Neo re-assess. The dispatch does not depend on it
(`expedite` does that work), so this only changes the `priority:` label and whether a
release is cut on landing.
