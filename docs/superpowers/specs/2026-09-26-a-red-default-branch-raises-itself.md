# A red default branch raises itself

wo-8736a5c5, GitHub issue #793. Design decided by Neo, question 739 — the five
deliverables below are that ruling, not a re-opening of it.

## The problem

Measured, 2026-09-26.

1. **The OS merged onto a stale base and broke `main`.** PR #765 (wo-15f5d969)
   auto-merged 21:31Z on a CI pass taken against a base that `#762` had moved in the
   meantime. `automerge.decide` condition 6 requires `CLEAN` and deliberately does not
   require an up-to-date branch — `src/jarvis/automerge.py:221-226` states why (the user
   left `strict_required_status_checks_policy` off, so `BEHIND` would make the mechanism
   never arm). The merge result failed `unit (3.13)` on `main`, run 36273250195 (issue
   #790).
2. **Nothing in the OS noticed for ~4h.** No attention item, no inbox row, no invariant
   or `jarvis doctor` finding. Every base-health fact the OS holds is written per work
   order, by the heal path, and only for an order whose own pull request is FAILING:
   `ops.record_base_health` (`src/jarvis/ops.py:5013-5036`) is called from
   `Daemon.heal_inherited_failure` (`src/jarvis/daemon.py:5901-5906`), reached only from
   the `elif pr.failing:` branch of the poll (`src/jarvis/daemon.py:4579-4597`). #765 had
   merged and left nothing parked, so there was no order for that path to run on at all.
3. **Auto-merge kept holding with a sentence that read as waiting.** Every parked order
   recorded `automerge.HELD_CHECKS_NOT_GREEN` whose reason is *"CI has not finished a
   unanimous pass on this commit"* (`src/jarvis/automerge.py:295-296`). For PR #789
   (wo-535e9510) CI had FINISHED and FAILED, on `unit (3.13)` — a test that pull request
   never touched. One code covers three different worlds (failed, still running, nothing
   reported), and `_note_automerge_held` dedupes on (sha, code, reason)
   (`src/jarvis/daemon.py:5759-5768`), so the sentence a user reads on
   `jarvis wo show` was both wrong and sticky.
4. **Nothing paused the merges.** With `main` red, an order whose own checks are green is
   armed, gated, approved and merged — onto a base that is already broken, which hides the
   next break behind the same red run.

Root cause of (2)-(4) in one line: **the OS has no fact "the default branch of project P
is red" — only "this pull request's base was red when its check ran", per work order,
written from a path that a green or unparked pull request never reaches.** The stale-base
merge in (1) is a SYMPTOM being left alone on purpose: requiring an up-to-date branch is a
different decision, argued against at `src/jarvis/automerge.py:221-226`, and out of scope
here (see Not covered).

## The fix

Five parts. One new project-level fact, read once per project per PR-poll tick; three
readers of it (an inbox row, an invariant, the merge decision); one hold code split.

### 1. `Daemon.poll_default_branch` — a new step on the PR-poll cadence

New method on `Daemon`, called from the `if poll_prs:` block of `tick`
(`src/jarvis/daemon.py:808-814`), BEFORE `poll_pull_requests` so the same tick's
`auto_merge` decisions read a fresh fact.

Run for EVERY project whose `github.origin_repo(project.path)` is not None
(`src/jarvis/github.py:160-180`). **Explicitly not only projects with a parked pull
request, and not only projects with `validation.auto_merge` on** — Neo's reason, which is
the incident: #765 merged and left nothing parked, so a check scoped to parked pull
requests would have seen exactly nothing. A project that has not opted into auto-merge
still wants to know its `main` is red.

Body:

1. resolve the default branch name (§6);
2. `ci.base_runs(base, cwd=project.path)` (`src/jarvis/ci.py:164-199`) — ONE call per
   project per PR-poll tick, the whole added cost, ~30 calls/hour/project at
   `PR_POLL_EVERY_TICKS = 24` (`src/jarvis/daemon.py:103`);
3. red iff any workflow's newest COMPLETED run is red — `ci.latest` +
   `Run.red` over the set of workflow names present in the rows
   (`src/jarvis/ci.py:271-276`, `112-122`). `ci.base_is_red` is reused with the workflows
   derived from the runs themselves rather than from a pull request's failing checks
   (`src/jarvis/ci.py:295-301`); `cancelled` is not red, and
   `src/jarvis/ci.py:82-85` is why;
4. write the fact (§2). On `github.GitHubError` write nothing and log at debug: an
   unreadable base is "not known", never green and never red.

No write to GitHub. No work-order event: the fact is not about a work order.

### 2. The fact: one JSON row in `CentralStore.os_state`

Key `base_health:<project>`, value JSON via `CentralStore.set_state` / `get_state`
(`src/jarvis/central_store.py:1349-1357`):

```json
{"red": true, "base": "main", "workflow": "ci", "run_id": 36273250195,
 "run_url": "https://github.com/…/actions/runs/36273250195",
 "head_sha": "<the commit on main>", "wo_id": "wo-15f5d969", "checked_at": 1790000000.0}
```

`run_url` is rendered from `(owner, repo)` and `run_id` by a new pure
`ci.run_url()`; `gh run list` answers `databaseId` and no URL
(`src/jarvis/ci.py:71`).

**`checked_at` is load-bearing.** The pause in §4 acts only on a reading no older than
`BASE_HEALTH_FRESH_SECONDS = 900` (~7 PR-poll intervals). A `gh` that stops answering
must not pause the fleet's merges for ever on a fact nobody can confirm, and 900s is
several polls, so a single transient failure does not resume merging onto a red `main`
either. Freshness is judged in the daemon; `decide` receives a fact or nothing.

**Inbox rows: transitions only**, exactly `ops.record_base_health`'s discipline and for
its reason (`src/jarvis/ops.py:5015-5020`) — a red `main` across a day would otherwise
write ~700 identical rows. Comparison is against the STORED state, so it is independent of
the invariant ledger's cadence.

* RED transition — `CentralStore.add_inbox(level="warning")`
  (`src/jarvis/central_store.py:456-462`), naming the workflow, the run URL, the head
  commit, and the work order when one is attributed (below). `level="warning"` because
  `route_new_inbox` has no level filter and this is news worth a ping.
* GREEN transition — one `level="info"` row, so the user learns it recovered, and
  `store.close_violation_reports` for the invariant's key (§3).

**Attributing the red commit to a work order cannot be done by sha, and the framing in
the work order is wrong here.** `automerge_merged` carries `head_sha =
decision.judged_sha` — the PULL REQUEST's head (`src/jarvis/daemon.py:4886-4894`) — while
the merge is `--squash` (`src/jarvis/automerge.py:394`), so the commit on `main` is a new
object with a different sha. The two never compare equal. So:

1. on the RED transition only, read the red run's head commit subject with a new
   `ci.commit_subject()` built in the shape `ci.VERBS` already allows —
   `["api", "--method", "GET", f"repos/{owner}/{repo}/commits/{sha}", "--jq",
   ".commit.message"]`, the same pinned-GET shape as `ci.commit_parents`
   (`src/jarvis/ci.py:262-265`), so no `VERBS` entry and no change to the AST test;
2. take `(#N)` off the squash subject, resolve N against work orders whose `pr_url` ends
   `/pull/N`, and claim the order ONLY if it carries an `automerge_merged` event — the row
   says "the OS merged this", so it must be true;
3. anything unreadable or unmatched names no work order. A red `main` with no attribution
   is still the row the user needs.

One extra `gh` call per red transition, never per tick.

Rejected: a new `project_store` table, or a synthetic work-order-less event surface —
more machinery for one boolean, and a per-project table cannot hold a fleet-shaped key.

### 3. `check_default_branch_green` — a DERIVED invariant, no network

Registered in **`INVARIANTS`** (`src/jarvis/invariants.py:3769`), not `OS_INVARIANTS`.
The registry rule is stated at `src/jarvis/invariants.py:28-33`: `OS_INVARIANTS` is for a
fact about the OS rather than about one project, takes no store, runs once per
`jarvis doctor` and **never on the daemon's reconcile tick**. `main` of project P is P's
fact, and this finding must reach the reconcile tick — that is the path that reports and
notifies (`src/jarvis/daemon.py:3818-3844`). The state living in the CENTRAL database is a
storage detail, not a change of scope: per-project invariants already open `CentralStore`
(`src/jarvis/invariants.py:2038`, `3739`). The project's name comes from
`CentralStore.project_name_for_path(store.project_path)`
(`src/jarvis/central_store.py:429-437`).

Derived, never fetched, under the rule `base_red_note` states
(`src/jarvis/invariants.py:1089-1102`): an invariant that shelled out to `gh` would put a
subprocess behind `jarvis wo list`. It reads `base_health:<project>`, yields one
`Violation` with `wo_id=None`, `repaired=False`, and a `detail` carrying the workflow, the
run URL, the merge commit and the attributed order.

**One writer per direction, so the user is not pinged twice for one break.** The
unrepaired violation's `store.add_notification` (`src/jarvis/daemon.py:3838-3844`) is the
RED row; the poll step writes no red row of its own. It is deduped by
`open_violation_report` per (invariant, wo_id) — which only closes on the landing-sweep
cadence, an hour (`src/jarvis/daemon.py:3823-3827`, `LANDING_SWEEP_EVERY_TICKS = 720`), so
a break-recover-break inside one hour would otherwise be silent. That is why the GREEN
transition in §2 calls `close_violation_reports` explicitly: recovery re-arms the report,
and the next break notifies again.

Not repairable, and it must not try: the remedy is a commit on `main`.

### 4. `automerge.HELD_BASE_RED` — the merge pauses while the base is red

Reason for pausing at all: merging onto a red `main` hides the next break behind the same
red run, and the OS's own merge is what broke it in #793.

New constant beside the others (`src/jarvis/automerge.py:99-113`) and a NEW PARAMETER on
`decide`: `base_red: BaseRed | None`, a frozen dataclass built by the daemon from the
stored JSON. `None` means "the OS holds no fresh fact" — no pause, no claim. `decide`
stays pure and the whole table stays in one function: **not** an early return in
`Daemon.auto_merge`, which is the shape `src/jarvis/automerge.py:204-213` and
`src/jarvis/daemon.py:4733-4750` both argue against.

**Position: after condition 3 (plan assumptions), before condition 4.** Three reasons,
and the third is the one that costs money:

* conditions 1-3 are about PERMISSION and OWNERSHIP and must dominate — a project that
  never opted in must record no hold at all (`src/jarvis/daemon.py:5746-5752`), and an
  assumption the user owes outranks a fact about the world;
* conditions 4-6 are about THIS pull request's readiness. Reporting "round 2 passed on
  a1b2c3d, the head is now e4f5a6b" while the OS would refuse the merge anyway sends the
  user to fix the wrong thing;
* `Daemon.auto_merge` re-judges a moved head only on `HELD_SHA_MOVED`
  (`src/jarvis/daemon.py:4801-4806`). Sitting before the sha checks means no round is
  spent re-judging a branch that cannot merge and whose CI is inheriting `main`'s
  failure — `ops.force_validation_state` names spending round numbers on a failing build
  as the thing to avoid (`src/jarvis/ops.py:4012-4014`). The re-judge is DEFERRED, not
  lost: nothing consumes the trigger, and the tick after `main` goes green produces the
  same `HELD_SHA_MOVED` and re-judges then.

**The "not this pull request's fault" sentence lives on THIS hold, not on the split
below**, and that is a deliberate refinement of the work order's framing: with the
condition placed here, a failing pull request on a red base never reaches the checks
conditions, so a rider attached there would be unreachable. `HELD_BASE_RED`'s reason
therefore says both things in one sentence:

> `main` itself is red — workflow `ci`, run <url>, at <commit> (merged by the OS for
> wo-15f5d969). Nothing merges onto a broken default branch. This pull request's own `ci`
> failure is the same break and not its fault.

The last clause is emitted only when a workflow in `pr.red` matches the stored red
workflow (`src/jarvis/github.py:322-325`). Matching is by WORKFLOW and never by job name,
for `ci.inherited`'s reason: fail-fast makes the base and the branch name different shards
of one matrix (`src/jarvis/ci.py:314-323`). The fact comes from the passed-in `base_red`,
so `decide` stays pure.

The pause does not touch the heal: `heal_inherited_failure` runs in an earlier branch of
the poll (`src/jarvis/daemon.py:4586-4597`) and is unaffected.

### 5. `HELD_CHECKS_NOT_GREEN` splits three ways

One code per condition is the rule (`src/jarvis/automerge.py:92-98`) and issue #263 is
what breaks without it. `checks_green` is false in three different worlds
(`src/jarvis/github.py:328-341`), so:

| code | condition | reason text |
|---|---|---|
| `HELD_CHECKS_FAILED = "checks_failed"` | `pr.failing` non-empty | `CI failed: unit (3.13), evals` |
| `HELD_CHECKS_RUNNING = "checks_running"` | any check's `status` in `github.UNFINISHED_STATUSES` (`src/jarvis/github.py:211-212`) | `CI has not finished on this commit — 2 check(s) still running` |
| `HELD_CHECKS_NONE = "checks_none"` | `not pr.checks` | `no check has reported on this commit` |

Failed wins over running: one shard can fail while its siblings queue, and the failure is
the actionable fact. `HELD_CHECKS_NOT_GREEN` is REMOVED — no reader keys on it
(`ops.force_validation_state` keys on `sha_moved`/`sha_unrecorded` only,
`src/jarvis/ops.py:4029-4038`; `invariants.rejudge_exhausted` and `automerge_denied` on
`sha_moved` only, `src/jarvis/invariants.py:533`, `584`), and rows already on disk render
from their stored `reason`, not from the code (`ops._automerge_line`,
`src/jarvis/ops.py:3183-3198`). Names in `pr.failing` are the OS's own read of GitHub's
rollup, not a remote's prose.

### 6. Recovery once `main` is green — ALREADY BUILT, and the honest gap

**Do not re-specify this.** The fleet-wide recovery issue #793 point (3) asks for exists:

* `Daemon.heal_inherited_failure` holds while the base is red and, the tick after it
  recovers, rebuilds the merge ref for EVERY parked pull request carrying a stale red
  check — the base's CI is read once per project and shared across the loop
  (`src/jarvis/daemon.py:4491-4498`, `5901-5914`);
* `ci.update_branch` is the only thing that clears an inherited failure, and a re-run
  provably is not (`src/jarvis/ci.py:202-231`, `13-20`);
* `ops.carry_validated_head` moves the verdict to the commit the update produced, proved
  from that commit's parents (`src/jarvis/daemon.py:5956-5981`,
  `src/jarvis/ci.py:234-265`), so the healed pull request merges instead of stalling;
* when the carry is refused — parents unreadable, head already moved, the read-back after
  the update failed (`src/jarvis/daemon.py:5947-5955`) — `decide` holds
  `HELD_SHA_MOVED` and `Daemon._rejudge_moved_head` opens a fresh round
  (`src/jarvis/daemon.py:4801-4806`, `5657-5686`).

**Gap: none for the recovery itself.** A `HELD_SHA_MOVED` after an `update-branch` cannot
strand an order silently. Either the re-judge runs, or `ops.rejudge_moved_head` declines
because the next round would be the last, records the decline, and
`invariants.rejudge_exhausted` raises attention off that plus the standing hold
(`src/jarvis/invariants.py:513-545`) — self-clearing when the branch moves again.

Two things the record must SAY, and they are all that is owed here:

1. the `HELD_BASE_RED` sentence (§4) names the base break, so a parked order reads as
   waiting for `main` rather than as failing its own CI — that is `ops.automerge_state`'s
   line, on `jarvis wo show`, the dashboard page and nothing else;
2. `status_label` is deliberately NOT extended. `invariants.base_red_note` answers in one
   indexed read of `pr_base_health` and its docstring refuses an unconditional second read
   per work order on every listing in the fleet (`src/jarvis/invariants.py:1089-1102`). An
   order whose own pull request is green while `main` is red therefore keeps the plain
   `waiting_pr_merge` label and carries the fact on its auto-merge line. Changing that
   would cost every listing a central read per order, for a sentence the merge line
   already gives.

### The default branch name

There is no helper today. **Chosen: a pinned GET through `ci.py`** —
`["api", "--method", "GET", f"repos/{owner}/{repo}", "--jq", ".default_branch"]`, the
shape `ci.VERBS` documents and the AST test enforces (`src/jarvis/ci.py:51-61`,
`tests/test_base_heal.py:646-665`). No new `VERBS` entry is needed: the allowlisted pair
is `("api", "--method")`. `(owner, repo)` from `github.origin_repo`, which is also the
step's gate. Cached in the same `base_health:<project>` JSON (`base`, `base_read_at`) and
re-read only when absent, so the steady-state cost stays the one `ci.base_runs` call Neo
costed.

Rejected: a local git read (`git symbolic-ref refs/remotes/origin/HEAD`). `origin/HEAD` is
set by `git clone` and is absent in worktrees and in repositories initialised in place —
including every fixture project (`src/jarvis/testing.py:2201` runs `git init -b main` with
no remote) — so the commonest answer would be "unknown", which is the answer that does
nothing. A pinned read of the repository is the fact itself.

Failure mode, both readings: **unreadable means NOT KNOWN — no pause, no inbox row, no
invariant finding, no claim in either direction.** `automerge.PROTECTION_UNREADABLE`'s
rule one fact along (`src/jarvis/automerge.py:420-438`): "unreadable" and "green" are
different facts and the first must never render as the second.

## Rejected alternatives

1. **Require an up-to-date branch in condition 6 (`BEHIND` holds).** The obvious fix for
   part (1) of the problem, and it loses here: with
   `strict_required_status_checks_policy` off, `CLEAN` is the ordinary state and `BEHIND`
   is common, so the mechanism would essentially never arm
   (`src/jarvis/automerge.py:221-226`). It also fixes nothing about the ~4h of silence,
   which is what the work order is for.
2. **Scope the check to projects with a parked pull request, or to `auto_merge` projects.**
   Neo, question 739: in #793 the OS merged and left nothing parked, so this would have
   seen nothing. It also makes a red `main` invisible to every project that has not opted
   into auto-merge.
3. **An early return in `Daemon.auto_merge` instead of a condition in `decide`.** Cheaper
   to write, and it puts one of the merge conditions outside the one table that is unit-
   testable without a network — the split
   `docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md` and
   `src/jarvis/automerge.py:204-213` exist to prevent.
4. **A new `project_store` table, or a work-order-less event surface, for the fact.** More
   machinery for one boolean, and the fact is fleet-shaped: it belongs beside
   `daemon_pid` and `catalog_path` in `os_state`.
5. **`OS_INVARIANTS` for the finding.** It would never run on the reconcile tick
   (`src/jarvis/invariants.py:28-33`), which is the only path that notifies — and it would
   report one project's repository as a fact about the OS.
6. **Keep one `checks_not_green` code and only reword it.** The dedupe keys on (sha, code,
   reason) (`src/jarvis/daemon.py:5759-5764`); one code over three conditions is exactly
   issue #263's shape, where the second condition to hold a commit is dropped as a repeat
   of the first.
7. **Re-run CI on `main` when it goes red.** Not this mechanism's business, and `ci.py`'s
   whole opening argument is that a re-run replays a fixed merge commit
   (`src/jarvis/ci.py:13-20`). A red `main` needs a commit.

## Tests

No network anywhere: the `fake_gh` fixture installs a fake `gh` binary and records every
argv (`src/jarvis/testing.py:1534-1552`), and `started`/`project`/`poll` are
`tests/test_base_heal.py:92-122`. Reuse them; new file `tests/test_base_red.py` for the
fleet-level behaviour, plus rows in `tests/test_automerge.py` for the pure table.

**Fixture work required, and it is part of the deliverable:**

* `FAKE_GH` rejects any `api --method GET` path that is not `/commits/` or
  `/protection` with exit 2 (`src/jarvis/testing.py:1250-1257`). Add two answers —
  `repos/{owner}/{repo}` → `.default_branch`, and `.commit.message` on the existing
  `/commits/` path — with handles `set_default_branch()` and `set_commit_subject()` beside
  `set_runs` / `set_parents` (`src/jarvis/testing.py:1738-1764`). The deliberate
  unhandled-argv exit stays for a THIRD shape.
* fixture projects have no `origin` (`src/jarvis/testing.py:2201`), and the new step is
  gated on one. Add an opt-in helper `with_origin(path, "acme/proj")` — matching the
  existing `PR = "https://github.com/acme/proj/pull/7"` so `checked_pr_url` still passes
  (`src/jarvis/github.py:139-158`). Do NOT add a remote to `make_git_project` for every
  suite: that arms the origin check in tests that never asked for it.

Per deliverable:

1. **`poll_default_branch`** — one `gh run list` per project per poll tick and nothing
   else (count `fake_gh.calls` with `argv[:2] == ["run", "list"]`, as
   `tests/test_base_heal.py:370` does); it runs for a project with NO parked pull request
   and with `auto_merge` off (the #793 shape — this is the test that would have caught the
   incident); it runs for no project without an `origin`; a `gh` that fails writes no
   state and raises nothing.
2. **The fact and the rows** — one warning row on the red transition naming workflow, run
   URL and commit; nothing on the next three polls with `main` still red; one info row on
   the green transition; the attributed work order appears when the squash subject carries
   `(#7)` and that order has an `automerge_merged` event, and NO order is named when the
   subject is unreadable or the sha does not match — including the regression this spec
   exists to prevent: attribution must not be attempted by comparing
   `automerge_merged.head_sha` with the base run's `headSha` (assert a red `main` whose
   run sha equals a judged sha attributes nothing on that basis).
3. **The invariant** — `check_project(store, repair=False)` yields the finding off stored
   state with `fake_gh.fail()` in force, proving no subprocess (assert `fake_gh.calls` is
   unchanged across the check); the notification is written once across repeated reconcile
   passes; the green transition closes the report, and a second break notifies again.
4. **`HELD_BASE_RED`** — pure rows in `tests/test_automerge.py` through its `decide`
   helper (`tests/test_automerge.py:63-74`): `base_red=None` arms; a fresh red fact holds
   with the new code; a stale fact (`checked_at` beyond `BASE_HEALTH_FRESH_SECONDS`) is
   passed as `None` by the daemon and arms; ordering — a red base with an assumption
   pending holds on `HELD_ASSUMPTIONS`, and a red base with a moved head holds
   `HELD_BASE_RED` and opens NO round (assert `latest_validation_round` unchanged, which
   is the round-number cost); the rider clause appears only when `pr.red` shares the
   stored workflow. `test_no_two_conditions_share_a_hold_code`
   (`tests/test_automerge.py:175-179`) and `test_decide_touches_no_store_no_clock_and_no_network`
   (`tests/test_automerge.py:246-255`) must both still pass — the second is what pins the
   new parameter as data rather than a store handle.
5. **The split** — replace the four `HELD_CHECKS_NOT_GREEN` rows at
   `tests/test_automerge.py:157-162` with three codes: `checks=()` → `checks_none`, a
   `FAILURE` → `checks_failed` with the check's NAME in the reason, `IN_PROGRESS` →
   `checks_running`, and failed-beside-running → `checks_failed`. Update
   `tests/test_base_heal.py:450-455`, whose assertion is the still-running case.
   End-to-end: a parked order held on a red `main`, then `main` green, then the heal, then
   the merge — the existing three-tick shape at `tests/test_base_heal.py:390-430` with a
   red base prepended, which proves the pause is a pause and not a stall.

## Surfaces that must change with the hold split

Every renderer of a hold, checked: none of them keys on `checks_not_green`, so the split
leaves no stale sentence — but all of them render the new reason text.

* `ops._automerge_line` (`src/jarvis/ops.py:3183-3198`) — generic `held — <reason>`. No
  change.
* `ops.automerge_state` (`src/jarvis/ops.py:2604-2670`) — picks the newest event; no code
  literals. No change.
* `ops.force_validation_state` (`src/jarvis/ops.py:3996-4040`) — diagnoses `sha_moved` and
  `sha_unrecorded` only, and says why a red build must NOT be worded as something to force
  a round over. Unchanged, and `HELD_BASE_RED` must not be added to it.
* `cli.py:2452-2453` and `ui/app.py:1042-1046` — both print
  `ops.automerge_state(...)["line"]`. No change.
* `invariants.rejudge_exhausted` (`src/jarvis/invariants.py:513-545`) and
  `automerge_denied` (`548-587`) — key on `HELD_SHA_MOVED`. Unchanged, and worth
  re-reading: with `HELD_BASE_RED` sitting before the sha checks, a red base writes a
  newer hold that is NOT `sha_moved`, which neither predicate reads as a clear. Assert
  that: a refused merge stays flagged across a red-base episode.
* `timeline.py:673-687` labels only the two post-merge kinds; `automerge_held` is
  `automerge_state`'s. No change.

## Not covered

* **The stale-base merge itself** (problem 1). Fixing the cause — requiring an up-to-date
  branch, or an `update-branch` before every armed merge — is a separate decision against
  `src/jarvis/automerge.py:221-226`. This work makes the CONSEQUENCE loud and stops it
  compounding; it does not stop the next stale-base merge.
* **Job-level naming of the base failure.** `gh run list` answers `workflowName` and no
  job (`src/jarvis/ci.py:71`), so rows and holds name the workflow and the run URL, not
  `unit (3.13)`. A job name would need `gh run view --json jobs`, a second call and a new
  `VERBS` entry, for a string the run URL already shows.
* **`status_label` / `base_red_note`.** Unchanged, for the per-listing read cost stated in
  §6.
* **Blocking DISPATCH while `main` is red.** Not asked for, and it would stop work that
  can proceed: a worker can develop against a red base; only the merge must wait.
