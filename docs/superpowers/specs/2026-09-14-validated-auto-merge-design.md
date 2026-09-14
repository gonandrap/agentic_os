# A pull request merges itself only if the panel read the commit that merges

*Design proposal, 2026-09-14. Nothing here is built. No code, no PR.*

*Reading order: `mem:work-order-lifecycle` (`waiting_pr_merge` and `poll_pull_requests`),
`mem:privileged-action-gates` (the `self_heal` precedent this design copies),
[2026-09-12-the-pull-request-is-the-artifact.md](2026-09-12-the-pull-request-is-the-artifact.md)
(what the panel actually judges),
[2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md](2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md)
(the poll loop this extends, and its budget).*

---

## 1. The problem

The user hand-merges every pull request the fleet produces. GitHub has a feature for
exactly that — auto-merge — and it is the wrong one, because of what it fires on.

GitHub auto-merge fires when **branch protection is satisfied**: the 5 required checks on
`protect-main` go green, and the PR merges. Jarvis's validation panel is not one of those
checks and has no way to become one without being told about it. So auto-merge, switched
on naively, merges a submission the panel has not read — and on this repo the panel *is*
the quality gate. CI runs the tests the submitter wrote. The panel is the reviewer that
never met the submitter (`validation.py`'s header), holds two vetoes (`VETO_SEATS =
("security", "tester")`), and is the only thing in the OS that reads a diff with an
adversarial eye. Merging on CI alone deletes the review and keeps the latency.

Two requirements, and they pull in opposite directions:

1. **The automatic merge fires only after the panel accepted AND CI is green.**
2. **The human merge is never blocked.** The user can merge any pull request at any
   moment, validated or not, exactly as they do today. The mechanism gates the *machine*,
   never the person.

Requirement 2 is what kills the obvious answer, as §3 shows.

### 1.1 The fact that decides the whole design

From GitHub's own documentation, verified 2026-09-14: auto-merge, once armed, is
disabled in exactly two cases —

* someone **without** write permissions pushes to the head branch, and
* someone switches the base branch.

A push by someone **with** write permissions leaves auto-merge armed. Every actor that
touches a Jarvis pull request has write permissions: the worker, the heal loop's nudged
worker, the user. So an armed auto-merge survives precisely the pushes we care about, and
merges whatever is at the head ref when the last check goes green.

This is not a hypothetical on this fleet. `Daemon.heal_pull_request` exists to make a
worker push to its own branch after it is parked — twice over, `ops.PR_CONFLICT` and
`ops.PR_CHECKS`. A pull request that passes the panel, goes red, gets nudged, gets a new
commit and goes green again is an ordinary Tuesday here, and with armed auto-merge it
lands code no seat has read.

---

## 2. What already exists (so the design adds as little as possible)

| the fact | where it lives | how it is read |
|---|---|---|
| the panel's verdict on a unit | `validation_rounds` row, `outcome='passed'`, one per round | `ProjectStore.latest_validation_round(wo_id=…)`; `jarvis wo show <id>`; `jarvis validation show <id>` |
| each seat's opinion | `validation_opinions` | `jarvis validation show <id>` |
| "did the submitter produce new evidence" | `validation_rounds.fingerprint` (`evidence.fingerprint`) | `Daemon._preceding_round` |
| the pull request's live state, CI included | `github.pr_view` → `PullRequest` (`state`, `mergeable`, `mergeStateStatus`, `statusCheckRollup`) | `Daemon.poll_pull_requests`, every `PR_POLL_EVERY_TICKS` (24, ~2 min) |
| where a passed round lands the work order | `ops.land_when_cleared` → `ops.land_finished` → `waiting_pr_merge` | — |
| the merge as a privileged act | `gates` kind `pr_merge`; `gh pr merge …` is a `SEED_CANARIES` entry that **can never be exempted** | `jarvis gate list/show/approve/deny` |
| a merge that already happened | `Daemon.poll_pull_requests` → `ops.complete_merged` | — |

Two facts do **not** exist yet and are the whole of the new state:

* **which commit the panel judged.** `validation_rounds` stores `fingerprint` and
  `pr_url`, and `evidence.fingerprint` deliberately excludes `head` — correctly, for the
  question it answers (§5.1). Nothing in the OS records the head SHA of a judged round.
* **permission for Jarvis to merge.** There is none, by design:
  `github.py`'s module docstring says "merging is a privileged action a human or a gate
  approval authorises, never something a poll loop does on its own", and
  `READ_ONLY_VERBS` + an AST test in `tests/test_github_artifact.py` enforce it.

---

## 3. The three options

### Option 1 — GitHub-native auto-merge, armed by Jarvis at the moment the panel passes

`ops.land_when_cleared` reaches `waiting_pr_merge` after a passing round; at that instant
Jarvis runs `gh pr merge --auto --squash <url>`. Before the pass, auto-merge is simply
never armed, so nothing can fire early.

**It is correct at the instant it is armed and wrong ever after.** §1.1 is the refutation:
the arming survives a worker's push. The heal loop pushes. Therefore the mechanism merges
unvalidated code on the one path the OS itself drives.

The obvious patch — have the poll notice the head moved and run `gh pr merge
--disable-auto` — loses the race it depends on. The poll runs every ~2 minutes; a push and
the subsequent CI-green can both land inside one interval, and GitHub merges the moment
the last check reports. A safety mechanism that is a 2-minute race against a CI run is not
a safety mechanism.

There is also a smaller, sharper problem with arming at pass time: `gh pr merge --auto` on
a PR whose checks are **already** green does not arm anything, it merges immediately — so
the same call means two different things depending on CI timing.

**Rejected.** No SHA binding is available to it, and a mechanism that can merge code the
panel never saw is worse than no mechanism.

### Option 2 — a required status check owned by Jarvis (`jarvis/validated`)

Jarvis posts a commit status against the PR **head SHA**: `pending` while a round is open,
`success` when the panel passes. `jarvis/validated` joins the 5 required checks on
`protect-main`. Repo auto-merge is then switched permanently on and is safe: it cannot
fire until Jarvis says so, and because a status is bound to a SHA, a new push arrives with
**no** `jarvis/validated` status at all and the PR stops being mergeable, automatically,
with no Jarvis code involved.

That SHA property is genuinely excellent and it is the idea worth stealing (§5 steals it).
Three things sink the option as a whole.

**It blocks the human merge — that is not an implementation detail, it is what a required
check *is*.** Branch protection applies to people. The user's "I still can go ahead and
manually merge" survives only via a **bypass actor** on the ruleset, which means: the
merge button reads "merge without waiting for requirements to be met", every manual merge
becomes an override, and the bypass the user gains is not scoped to `jarvis/validated` —
it bypasses the three unit jobs, `evals` and `browser` too. Requirement 2 is met on a
technicality and degraded in practice.

**It brings the repository down when Jarvis is off.** A pull request the user opens by
hand from their own branch is governed by no work order, so no round ever runs and
`jarvis/validated` is never posted: that PR is unmergeable, for ever, without a bypass.
The fix — Jarvis enumerating every open PR and posting `success` on the ones it does not
own — means `gh pr list` per project per tick and Jarvis writing statuses onto pull
requests it has nothing to do with. And the coupling runs the wrong way permanently:
`jarvis config set … validation.enabled false` would brick every open PR in the repo until
someone edits the ruleset on GitHub. A local flag whose *off* state breaks the repo is a
bad flag.

**It creates a second source of truth.** The round table says one thing; a status posted
against a SHA says another, and they can disagree — a retracted round, a config change, a
status posted by a daemon that has since been rolled back. `jarvis validation show` and
the GitHub checks tab would both claim to be the record.

**Rejected as the mechanism.** Its SHA idea is adopted; its enforcement point is not. A
non-required advisory status is a cheap optional garnish — see §9.1.

### Option 3 — Jarvis merges it itself, through the gate — **RECOMMENDED**

Repo auto-merge stays **off**. `protect-main` is untouched. The daemon's existing PR poll,
which already reads state, mergeability and CI in one `gh pr view` per parked pull
request, gains one branch: when a work order is parked behind a pull request that is open,
`CLEAN`, green, **and** whose head SHA is exactly the SHA the latest — passing — validation
round judged, Jarvis asks for a `pr_merge`-class gate approval and, once granted, runs the
merge itself, pinned to that SHA.

---

## 4. Recommendation and why

**Build option 3.** The user gets the behaviour they asked for — pull requests that merge
themselves once validated and green — without the GitHub feature, and without touching a
single repository setting.

The reasons, in the order they matter:

**1. It is the only option that leaves the human merge literally untouched.** Nothing about
branch protection changes. The user merges in the GitHub UI, or with `gh pr merge`, on any
pull request, validated or not, with no override, no bypass actor, no second click and no
warning banner. The mechanism adds a way for the *machine* to do what the human was doing;
it subtracts nothing from the human. Options 1 and 2 both answer requirement 2 with
"well, technically" — and requirement 2 is half the ask.

**2. Turning it off is a local flag, not a GitHub edit.** `jarvis config set <project>
validation.auto_merge false` stops it instantly, and because the key matches
`catalog.SAFETY_KEYS`'s `*.validation.*` glob it comes with a mandatory `--reason` and a
recorded config version. If the daemon dies, if the panel breaks, if the user changes their
mind at 2am — the failure state is "PRs sit there waiting for a human", which is exactly
today's behaviour. Option 2's failure state is a repository whose pull requests cannot be
merged.

**3. The decision stays where the evidence is.** The round row, the seat opinions, the
fingerprint, the gate ledger and the timeline are all in the OS, reachable through
`jarvis wo show` / `jarvis validation show` / `jarvis gate show`. Nothing is exported to a
GitHub status that can drift from it.

**4. Branch protection becomes a backstop rather than a dependency.** `gh pr merge` on a
pull request that does not satisfy `protect-main` is refused *by GitHub*. So "CI is green"
is enforced twice — once by Jarvis's own `checks_green` predicate and once by the ruleset —
and the second one holds even if the first is wrong. Options 1 and 2 make the ruleset the
only enforcement.

**5. Leaving `allow_auto_merge: false` is itself a safety property.** `gh pr merge
--squash` against a pull request whose required checks have not finished does not fail —
`gh`'s own help says "If required checks have not yet passed, auto-merge will be enabled."
With repo auto-merge disabled, that path errors out loudly instead of silently arming a
timer. A bug in Jarvis's green-check logic therefore produces a failed merge attempt on the
timeline, not a delayed unvalidated merge.

**6. It is small.** One new module, one column, one gate kind, one config flag, ~5 lines in
the poll loop. Option 2 needs all of that *plus* a GitHub write path, a whole-repo PR
enumeration, and three repository setting changes.

**The honest cost, stated plainly:** the OS holds merge authority. That is a real
expansion of what Jarvis may do, and §7 is the objection section. The containment is that
the merge is a gated action reviewed by Neo, escalable to the user, recorded in the
approvals ledger, and switched off by default.

**The other cost:** latency. GitHub auto-merge merges the instant the last check reports;
this merges at the next poll tick, up to ~2 minutes later. The fleet already lives on that
cadence — `poll_pull_requests` is how a merged work order gets completed today — so the
user's experience changes from "I merge it, and ~2 minutes later Jarvis notices" to
"~2 minutes after it goes green, it is merged and closed".

---

## 5. SHA binding — the crux

A verdict is about a diff. GitHub merges whatever is at the head ref **at merge time**. If
those two differ by one commit, the OS has merged code no reviewer read, and every other
part of this design is decoration.

### 5.1 Why `fingerprint` cannot answer this, and must not be changed to

`validation_rounds.fingerprint` is a content hash over the pre-truncation diff, the side
effects and the normalised declared evidence. `evidence.fingerprint`'s docstring names its
exclusions and gives the reason for each, in a table whose first row is:

> | adds an empty commit | changes `head` | no new evidence |

That is right for the question the fingerprint answers — *did this submitter produce new
evidence?* — and it is exactly wrong for ours — *which commit did the panel read?* The two
questions have opposite answers on an empty commit, so one field cannot serve both.
**Do not add `head` to the fingerprint.** It would make every unchanged resubmission look
new, break `_preceding_round`'s repeat guard, and change the hash of every round already
on the record.

Nor can we re-collect the packet at merge time and compare fingerprints. It is tempting —
it would even catch a force-push that restores identical content, which a SHA comparison
flags as a change. It fails on two counts. It costs a full `gh pr diff` plus an
`ARTIFACT_FIELDS` `gh pr view` (body, files, the lot) on every poll tick for every parked
pull request, which is precisely the budget `poll_pull_requests`'s docstring pins with a
statement-counting test. And it is **weaker** where it matters: two different commits can
produce the same diff against different bases, so an identical fingerprint does not
establish that the thing GitHub is about to squash is the thing the seats read.

### 5.2 What is stored instead

A new column, `validation_rounds.head_sha`, written once per round from the packet the
seats actually got:

```
packet.source == "pull_request"  ->  head_sha = packet.pr.head_sha   (gh's headRefOid)
packet.source == "worktree"      ->  head_sha = ""
```

`""` is the fail-closed value and it reads as "not recorded". A worktree packet is a round
the panel judged against a local diff with no pull request behind it, or one whose PR
fetch failed and fell back (`packet.pr_error`) — in both cases nothing binds the verdict to
a commit, so nothing may auto-merge. Every `validation_rounds` row that predates the
column migrates to `''` and is likewise never auto-mergeable, which is the correct reading
of rows written before anyone recorded the fact.

The predicate has exactly one home:

```python
ProjectStore.validated_head(wo_id) -> str | None
    # the head SHA of the LATEST round, if and only if that round's outcome is 'passed'
    # and its head_sha is non-empty. None otherwise.
```

"The latest round passed", not "some round passed". A work order that passed round 1, was
sent back, and is sitting on a pending round 2 is **not** validated. Deriving it at one
site is the `arbitrate` lesson applied again: a rule spread across three call sites is a
rule that holds by luck.

### 5.3 What makes it airtight rather than merely likely

Comparing `validated_head(wo_id)` against `pr.head_sha` from the poll's `gh pr view` closes
the case that matters — a push between the round and the poll. It leaves a window of
milliseconds between the `view` and the `merge`.

GitHub closes that window for us:

```
gh pr merge <url> --squash --delete-branch --match-head-commit <the judged SHA>
```

`--match-head-commit SHA` is the API's `sha` parameter: *"Commit SHA that the pull request
head must match to allow merge."* If the head has moved by so much as one commit, GitHub
refuses and merges nothing. **The merge is therefore conditional on the judged commit at
the server, not on Jarvis having looked recently.** The comparison in §5.2 exists so the
OS can *say* why it is holding; `--match-head-commit` is what makes it true.

Three independent things must all agree before a single byte lands on `main`: Jarvis's
stored SHA, GitHub's head at view time, and GitHub's head at merge time. Any disagreement
merges nothing.

### 5.4 What happens after a push that invalidates a pass

Nothing dramatic, and deliberately no attention flag. The poll sees `pr.head_sha !=
validated_head(wo_id)`, writes one `automerge_held` event carrying both SHAs (deduped per
head SHA, so a parked pull request does not accrue an event every two minutes), and
declines. `jarvis wo show` says so:

```
auto-merge: held — round 2 passed on a1b2c3d, the head is now e4f5a6b
```

It does **not** re-open validation. The way to a new verdict is the way that already
exists: the worker resubmits with `jarvis wo finish`, which opens round 3. If the worker
pushes without resubmitting — the ordinary outcome of a heal-loop nudge — the pull request
simply never auto-merges and the user merges it by hand, which is today's behaviour. Fail
closed, quietly, visibly.

A held auto-merge is not an attention item, because a heal-loop push is normal and the
user's list is not a place to put normal.

---

## 6. The manual escape hatch

There is nothing to escape. Stated as flatly as possible, because it is the design's main
selling point over option 2:

* `protect-main` is not edited. The 5 required checks are unchanged. No bypass actor is
  added. The merge button behaves identically for the user on every pull request in the
  repository, whether a work order governs it or not, whether the panel passed, rejected,
  escalated or never ran.
* Repository auto-merge stays **disabled**, so nothing on GitHub is armed and waiting.
* When the user merges by hand, the existing poll sees `MERGED` and completes the work
  order through `ops.complete_merged` — unchanged, already shipped, already the path.
* The machine half is off by default (`validation.auto_merge = false`) and is turned off
  per project or fleet-wide with `jarvis config set`, taking effect on the next tick.
* A pull request currently held (SHA moved, gate pending, Neo escalated) is merged by the
  user at any moment with no interaction with Jarvis at all. Jarvis notices afterwards, the
  same way it notices every merge today.

---

## 7. Failure directions — every one of them ends in "nothing merged"

| what breaks | what happens |
|---|---|
| the daemon is down | nothing polls. No merge. PRs sit exactly as they do today. |
| `gh` is not on the service's PATH | `github.GhUnavailable` → the existing `_warn_pr_poll_broken` path warns once per project per daemon run. No merge. |
| `gh` runs but has no credentials | plain `github.GitHubError`, same path. No merge. |
| `gh` has read credentials but no **write** scope | the merge attempt fails; `automerge_failed` on the timeline with the reason; after 3 attempts on one head SHA, one inbox row. No merge. (This is the most likely first failure — see §9.) |
| **the panel died silently on a usage limit (issue #235)** | the round is `pending` or `failed` — never `passed`. `validated_head` returns None. No merge. This is why the predicate is "the latest round passed" and not "no round was rejected": a panel that never answered must read as *not validated*, not as *not rejected*. |
| the validator is not wired in at all | `_validate_work_order` already closes the round `failed` with `NO_VALIDATOR_REASON`, explicitly never `passed`. No merge. |
| `validation.enabled` is switched off mid-flight | the merge branch requires `cfg.enabled` **as well as** `cfg.auto_merge`, re-read every tick. Turning the panel off stops auto-merge fleet-wide immediately, even for rounds that had already passed. |
| the round is old and `head_sha` is `''` (pre-migration, or a worktree packet) | not armed. "Not recorded" never reads as "matches". |
| GitHub has not computed mergeability (`mergeStateStatus: UNKNOWN`) | hold, retry next tick. |
| checks are queued, not finished | `PullRequest.checks_green` already requires a finished unanimous pass, not "nothing currently failing". Hold. |
| a required check regressed between the view and the merge | GitHub refuses the merge (branch protection). `automerge_failed`. No merge. |
| someone pushed between the view and the merge | `--match-head-commit` refuses at the server. No merge. |
| Neo is down, or escalated the gate | no grant exists → no merge. The request shows in `jarvis gate list --pending`; an escalated one reaches `jarvis status` and `jarvis gate approve <id>`. |
| the merge fails repeatedly | capped at `AUTO_MERGE_MAX_ATTEMPTS = 3` per head SHA, then one inbox row naming the pull request. The work order stays `waiting_pr_merge`; the hand merge still works. |
| the work order is in `needs_review` | not polled for merging. `needs_review` means a human owes a decision — a panel escalation, a closed pull request, a pending assumption — and the machine does not merge over a human's outstanding decision. |
| a pull request was closed and reopened | `pr_state` is stale by construction; the branch keys on the live `pr.state` from this tick's view, as the rest of the poll already does. |

Every row is "nothing merged". There is no row where a failure produces a merge. That is
the required direction, and it falls out of the structure — the merge needs six positive
facts, so any missing fact is a hold — rather than out of an exception handler someone
remembered to write.

---

## 8. Implementation sketch — by file and symbol

Nothing below is written. Sizes are rough.

**`src/jarvis/github.py`** (~10 lines) — stays read-only; `READ_ONLY_VERBS` untouched.
* `PR_FIELDS` and `ARTIFACT_FIELDS`: add `headRefOid` to both.
* `PullRequest.head_sha: str | None` and `PullRequestArtifact.head_sha: str`, populated in
  `pr_view` and `pr_artifact`.
* The two field sets still differ (`body`, `files`, the `gh pr diff` round trip are still
  artifact-only), so `tests/test_github_artifact.py`'s "they are not the same list"
  assertion holds. One cheap scalar is not the convergence that test forbids.

**`src/jarvis/evidence.py`** (~12 lines, mostly docstring)
* `judged_head(packet) -> str`: `packet.pr.head_sha` when `packet.source == "pull_request"`,
  `""` otherwise.
* `fingerprint` is **not touched**, and its docstring gains a line pointing at
  `judged_head` so the next reader does not try to merge the two.

**`src/jarvis/project_store.py`** (~40 lines)
* `ADDED_COLUMNS["validation_rounds"]`: `head_sha TEXT NOT NULL DEFAULT ''`.
* `set_validation_head(round_id, sha)` — one UPDATE, called once per round whatever the
  outcome, so a rejection also records what it was judging.
* `validated_head(wo_id) -> str | None` — §5.2's single predicate.

**`src/jarvis/catalog.py`** (~6 lines)
* `ValidationConfig.auto_merge: bool = False`, parsed in the existing validation block.
  It needs no `SAFETY_KEYS` entry: `*.validation.*` already covers it, which is the right
  answer — this changes what the OS is permitted to *do*.

**`src/jarvis/gates.py`** (~35 lines)
* New kind `AUTO_MERGE = "auto_merge"` in `KINDS`/`KIND_NAMES`, with **no** `SEED_MATCHES`
  and **no** canaries: `classify` returns it for no string, filed programmatically only,
  pinned by a test exactly as `SELF_HEAL` is.
* It does not ride `GateConfig.enabled`, for `self_heal`'s reason: it is the only thing
  between a verdict and an irreversible act, so it is mandatory rather than opt-in.
* `apply_decision`: one more arm routing an `auto_merge` verdict to
  `automerge.record_verdict` instead of `queue_message`. **This is load-bearing** — the
  worker finished long ago and a queued message would start a new turn on a work order
  nobody asked to reopen, which is the precise act `self_heal` is fenced against.
* Why a separate kind rather than reusing `pr_merge`: the grant scope is
  `(wo_id, kind, exact command)`, so an `auto_merge` grant provably cannot clear a
  worker's own `gh pr merge` — `classify` returns `pr_merge` for that string and never
  `auto_merge`. The two kinds share a command spelling and cannot share authority. Worth a
  test in both directions.

**`src/jarvis/automerge.py`** — NEW, ~190 lines. Mirrors `remedies.py`'s shape
(`decide`/`propose`/`record_verdict`/`apply`) because it is the same situation: the OS
acting on something nobody asked it to touch.
* `decide(round_row, wo, pr, cfg) -> Decision` — **pure**: dicts in, armed-or-held-with-a-
  reason out. No store, no clock, no `gh`. Pure for `arbitrate`'s reason: the whole
  condition table is then unit-testable without a network, and the safety rule lives in
  code rather than in a sequence of `if`s spread through a 180-line daemon method. The
  conditions, all of which must hold:
  1. `cfg.enabled and cfg.auto_merge`
  2. `wo["status"] == "waiting_pr_merge"`
  3. no pending assumptions (redundant with 2 via `land_when_cleared`; re-checked because
     the two gates are independent judgements and the redundancy is the point —
     [2026-09-13-two-gates-not-a-chain.md](2026-09-13-two-gates-not-a-chain.md))
  4. the **latest** round's outcome is `passed`
  5. that round's `head_sha` is non-empty and equals `pr.head_sha`
  6. `pr.state == "OPEN"`, `pr.mergeable_now`, `pr.checks_green`, and
     `pr.merge_state == "CLEAN"`
  On `CLEAN`: it means mergeable with every requirement satisfied. `UNSTABLE` (mergeable,
  a non-required check failing) and `BLOCKED` (a requirement outstanding) both hold.
  `CLEAN` is the ordinary state for this repo *because* the user disabled
  `strict_required_status_checks_policy` — with strict on, an up-to-date-but-behind branch
  reports `BEHIND` and would essentially never arm.
* `propose(store, neo, project, wo, decision)` — files the `auto_merge` approval plus its
  Neo question, `remedies.propose`'s shape. The justification carries the round number, the
  outcome, the judged SHA, the names of the green checks and the work-order title, so the
  reviewer reads the case rather than re-deriving it. Note `gates.file_request` only
  re-statuses a `running`/`dispatching` work order, and this one is `waiting_pr_merge`, so
  the status is untouched — verify, do not assume.
* `record_verdict(...)` — what `gates.apply_decision` calls. On `approved` it **records
  only and merges nothing**: the next poll finds the usable grant and merges, which means
  the SHA is re-verified *after* the approval. A grant lives an hour (`GRANT_TTL_SECONDS`);
  a verdict that merged inline would merge on an hour-old view.
* `apply(store, wo, sha)` — refuses unless `usable_grant` yields a live grant, spends it
  through `gates.open_gate` (never by hand — `remedies.apply`'s note), then performs **the
  one write**:
  `gh pr merge <url> --squash --delete-branch --match-head-commit <sha>`.
* `WRITE_VERBS = (("pr", "merge"),)` declared at module top, with an AST test asserting
  this module builds no other `gh` command — the same mechanism `github.READ_ONLY_VERBS`
  uses, applied to the one module that is allowed to write. `bugreport.create_issue` is the
  precedent for a `gh` write living outside `github.py`; keeping it out is what lets
  `github.py` go on claiming that everything in it is a question, which the panel's blind
  review rests on.
* `AUTO_MERGE_MAX_ATTEMPTS = 3`, counted per head SHA from `automerge_failed` events.

**`src/jarvis/daemon.py`** (~50 lines)
* `poll_pull_requests`: in the final `else` (open, not conflicting, not failing), after the
  repair clears, one call to `self.auto_merge(project, store, wo, pr)`.
* `Daemon.auto_merge`: `automerge.decide(...)`; on held, the deduped `automerge_held` event;
  on armed, `store.usable_grant` → `automerge.apply` → `ops.complete_merged`, or
  `automerge.propose` when no grant and no request is open.
* **The docstring's budget paragraph must be updated and `tests/test_pr_checks.py`'s
  statement count raised, deliberately.** The `else` branch gains one indexed read
  (`validated_head`) always, and one more (`usable_grant`) only when armed. That test
  counting statements is a feature — this design should be forced to declare its cost.

**`src/jarvis/ops.py`** (~35 lines)
* `complete_merged`: an `automerge_merged` event first, carrying the approval id, the round
  id and the SHA, so the record never reads as though a human merged it. The precedent is
  explicit in that function's own docstring (`pr_merged` vs `marked_done`: "the record must
  not claim the user did something they did not do") and it now cuts the other way too.
* `automerge_state(wo_id) -> dict` — what the CLI and the dashboard render.

**`src/jarvis/cli.py`** (~20 lines) — `jarvis wo show` gains one line and a `--json` key;
`jarvis validation show` prints each round's judged SHA. No new command: an armed
auto-merge is a property of a work order, not a noun of its own.

**`src/jarvis/ui/`** (~10 lines) — the same line on the work-order page.

**Tests** (~400 lines) — `tests/test_automerge.py` (every row of `decide`'s table, both
directions), plus additions to `tests/test_pr_checks.py` (the new branch and the raised
statement count), `tests/test_gates.py` (nothing classifies into `auto_merge`; an
`auto_merge` grant does not clear a worker's `gh pr merge`; the verdict does not message
the worker) and `tests/test_github_artifact.py` (the field sets still differ; `automerge`'s
verbs are declared and minimal).

**Total: ~400 lines of source, ~400 of tests. One new module, one column, one gate kind,
one config flag, five lines in the poll loop.**

---

## 9. What the user must change on GitHub

**For the recommendation: nothing.** That is the point. Specifically, do **not** run:

```bash
# NOT needed, and deliberately not wanted — see §4 reason 5.
gh api -X PATCH repos/gonandrap/agentic_os -f allow_auto_merge=true
```

Leaving it `false` is a safety property: it makes a premature `gh pr merge --squash` fail
loudly instead of silently arming a timer.

`protect-main` (ruleset 18487716) stays exactly as it is — 5 required checks, a pull
request required, no bypass actors, `strict_required_status_checks_policy: false`.

The one thing to **verify** (not change), because it is the most likely first failure:
every `gh` call the OS makes today is a read, so nobody has ever exercised write scope from
the daemon's environment.

```bash
# 1. Does this account have push rights on the repo?
gh api repos/gonandrap/agentic_os --jq '.permissions'
#    expect: {"admin":true,"maintain":true,"push":true,...}

# 2. Does the token the *daemon* uses carry them? The service environment is a different
#    environment from the user's shell (github.GhUnavailable's note, issue #90).
systemctl --user show jarvisd -p Environment
gh auth status

# 3. Confirm the merge would be permitted, without performing it:
gh pr view <url> --json mergeStateStatus,mergeable,headRefOid,statusCheckRollup
```

### 9.1 Optional garnish: an advisory status

Independent of everything above, Jarvis could post a **non-required** commit status so the
panel's verdict is visible on the pull request page:

```bash
gh api -X POST repos/gonandrap/agentic_os/statuses/<head-sha> \
  -f state=success -f context=jarvis/validated \
  -f description='panel passed round 2' \
  -f target_url='http://127.0.0.1:8787/wo/<wo-id>'
```

Display only: not in `protect-main`, so it blocks nobody and Jarvis-less pull requests are
unaffected. It costs a GitHub **write** verb for the sake of a badge, which is why it is
not part of the recommendation — but it is the cheap half of option 2, and if the user
wants to see the panel's verdict where they merge, this is how, with none of option 2's
consequences.

### 9.2 The commands for the rejected options, for completeness

Option 1 needs only the repo flag:

```bash
gh api -X PATCH repos/gonandrap/agentic_os -f allow_auto_merge=true
```

Option 2 needs that plus a read-modify-write of the ruleset — the API takes the whole
body, so it is not a one-liner:

```bash
gh api repos/gonandrap/agentic_os/rulesets/18487716 > /tmp/rs.json

# add jarvis/validated to the required checks
jq '(.rules[] | select(.type=="required_status_checks")
     | .parameters.required_status_checks) += [{"context":"jarvis/validated"}]' \
   /tmp/rs.json > /tmp/rs-checks.json

# and a bypass actor, or the user can no longer merge by hand.
# VERIFY the repository-role id before applying — it is not stable folklore.
jq '.bypass_actors += [{"actor_id":5,"actor_type":"RepositoryRole","bypass_mode":"always"}]' \
   /tmp/rs-checks.json > /tmp/rs-final.json

gh api -X PUT repos/gonandrap/agentic_os/rulesets/18487716 --input /tmp/rs-final.json
```

That second `jq` is the whole objection to option 2 in one command: making the machine safe
requires handing the human a permanent override of every check in the repository.

---

## 10. Open questions for the user

1. **`--delete-branch`?** Proposed on, matching what a hand merge usually does. It deletes
   the remote branch only; the worker's local worktree is untouched.
2. **Should Neo review every merge?** The design says yes (§8), for the audit trail and the
   escalation path, at a cost of one headless call and ~30s per merged PR. The alternative
   — the panel's pass *is* the authorisation, and the approval row is filed decided —
   halves the latency and loses the second opinion. The recommendation takes the review;
   this is the one knob worth arguing about.
3. **Feature orders.** A feature order validates as a whole (`ValidationConfig.feature_units`).
   This design governs work-order pull requests only. Whether a child's PR should wait for
   the parent feature's verdict is a real question and deliberately out of scope here.
4. **Fleet-wide or this repo only?** `os.validation.auto_merge` exists as a key but should
   ship `false`, with `agentic_os` opting in — the same posture every safety switch in this
   OS ships with.
