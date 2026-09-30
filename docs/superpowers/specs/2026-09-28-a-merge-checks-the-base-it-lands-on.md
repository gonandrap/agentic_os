# A merge checks the base it lands on

GitHub issue #837, wo-37f2fc2c. Neo question 987 chose option (b); this is HOW.
Predecessor this EXTENDS: `docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md`
(the carry §3, the catch-up §5). Also assumed: kn-907c9a61 (ancestry, not `.behind`, is
the authoritative behind-ness test on this repository).

## 1. The problem

**Nothing between `automerge.propose` and `gh pr merge` asks whether the judged commit
still contains the tip of `main`. So the OS squashes a branch whose CI never ran against
the code it is landing on, and `main` goes red.**

Third red `main` in two days from one shape: #790, #795, now #829 — after #806 and #793
shipped, both of which were about the same neighbourhood and neither of which closed this.

Gate 308, the measured case:

| fact | value |
|---|---|
| gate | 308, `auto_merge`, PR #829, wo-672bd388 |
| command | `gh pr merge … --squash --match-head-commit fa85f88ccd…` |
| filed | 2026-09-28 08:11:32 PDT (ts 1790608292.8) |
| main moved | #827 landed 08:02:03 PDT — **before the request was filed**; fa85f88 does not contain it |
| Neo answered | 12:10:29 PDT (ts 1790622629.36) — 3.98h later |
| main moved again | #830 landed 12:11:24 PDT — **between the verdict and the merge** |
| squash landed | 12:13:58 PDT |
| result | CI red on `main`; `INV-BASE-BRANCH-RED` raised it in 28s |

**The issue's premise is half wrong and the spec corrects it.** "Approved ~08:00, executed
12:13" is not what happened: the 4.3h is NEO REVIEW LATENCY, not execution latency. The
grant's own TTL (`gates.GRANT_TTL_SECONDS = 3600`, clock started at `decided_at` —
`ProjectStore.decide_approval`, src/jarvis/project_store.py:4796) never came close to
binding, and **shortening it would not have prevented this**: apply ran ~3 minutes after
the verdict, well inside any TTL worth having.

Staleness enters at TWO points, and each needs its own answer:

1. **Before the request is FILED.** #827 landed nine minutes before gate 308 existed. The
   packet Neo read was already false when it was written.
2. **While the request WAITS.** #830 landed 55 seconds after the verdict and 2.5 minutes
   before the merge. No TTL closes a window that small; only a check at the moment of
   merging does.

Root cause, stated once — and **the ancestry test answered on a stale fact, so none of the
seven guards ever fired** (established from wo-672bd388's own timeline):

* **`pr.base_oid` IS NOT A READING OF THE BASE. It is a reading of GitHub's own cached
  bookkeeping (`baseRefOid`), and it can be hours behind.** The `pr_base_updated` event at
  ts 1790607552 (2026-09-28 14:59:52Z) recorded `base_sha 8ea18feec4…`, taken from the
  FRESH `gh pr view` that `_catch_up_with_base` makes immediately before updating. `main`'s
  tip at that instant was `6b302c79f6` (#815, merged 14:59:23Z); `2473a8c0bd` (#828,
  14:56:03Z) had also landed, and `37fe650fab` (#827) landed two minutes later at
  15:02:03Z. `8ea18fe` is #821, merged 09:39:36Z — so GitHub's `baseRefOid` **lagged the
  real tip of `main` by three commits and 5.3 hours.**
* **Consequence:** at the 08:11 PDT propose tick, `ops.catch_up_needed` computed
  `is_ancestor(8ea18fe, fa85f88)` — TRUE, because fa85f88 IS 0f45386 plus a merge of main
  at 8ea18fe — and answered "not needed". The catch-up returned the pull request unchanged,
  control fell through, and `propose` filed gate 308 on a head that did not contain #827.
  No guard refused; the question was asked about the wrong commit.
* **`automerge.decide` (src/jarvis/automerge.py:204) contains no base-freshness fact at
  all.** Six conditions plus `base_red`; every one of them is about the PULL REQUEST or
  about the world's redness, none about what the pull request's base is now. So the OS
  arms, proposes and arms again on a branch that is behind.
* **`--match-head-commit` pins the HEAD, not the BASE** (`automerge._merge_args`,
  automerge.py:430). Neo's verdict text on 308 said "PR #829's head is still fa85f88" —
  true, and irrelevant. The head had not moved. The base had.
* **The #806 catch-up DEFERS BY FALLING THROUGH TO PROPOSE.** `Daemon._catch_up_with_base`
  (src/jarvis/daemon.py:6193) is reached from `Daemon.auto_merge` only on the
  `approval is None` branch (daemon.py:5232), and every one of its seven guards returns
  `pr` UNCHANGED. The caller's test is `if caught_up.head_oid != pr.head_oid: return` —
  so a guard that refused the update is indistinguishable from "already up to date", and
  control falls straight through to `automerge.propose`. `ops.catch_up_attempts >=
  ops.CATCH_UP_MAX` (ops.py:5825, cap 3) and `ops.base_heal_spent` are the two guards that
  refuse *permanently*, and either of them files a stale-base merge request.

Part 3 still makes every one of the seven a HOLD rather than a fall-through — the
fall-through is a real defect and the next stale fact must not be able to use it — but it
is the second half of the fix, not the first.

The authoritative test already exists and must not be rewritten: `ops.catch_up_needed(pr,
repo=…)` (src/jarvis/ops.py:5842) — `pr.behind` as a positive short-circuit only, then
`branchproof.is_ancestor(repo, base_oid, head)`, because this repository reports CLEAN for
a merely-behind branch (`strict_required_status_checks_policy` off). kn-907c9a61. What it
is asked ABOUT is what changes: see Amendment B.

## 2. Rejected alternatives

* **Shorten `GRANT_TTL_SECONDS`.** Measured above: the window that killed #829 was 2.5
  minutes. A TTL short enough to catch it would expire every honest grant.
* **Re-check the base in `record_verdict`, at approval time.** #830 landed AFTER the
  verdict. Checking there answers the wrong instant.
* **Re-use the `gh pr view` the poll already made for the ancestry fact inside `apply`.**
  That read can be minutes old on a slow tick, and #830 landed inside exactly that window.
  The check must be a freshly-read local fact (§3.1).
* **Serialise merge execution per project (issue item 4).** See §8: part 1 subsumes it.
* **Make `decide` read git itself.** `decide` is PURE — no store, no clock, no `gh`, no
  local git — and that is what makes its whole condition table unit-testable. The ancestry
  answer arrives as a parameter, the way `base_red: BaseRed | None` already does.
* **A second copy of the ancestry rule inside `automerge`.** `ops.catch_up_needed` is the
  one home; a second reader is how the pair comes to disagree (kn-907c9a61,
  `ProjectStore.validated_head`'s one-home discipline).

## 3. The fix

Five parts. Part 1 is load-bearing and the other four exist so it almost never has to fire.

### 3.1 Part 1 — an ancestry precondition immediately before `gh pr merge`

In `automerge.apply` (automerge.py:705), AFTER the four existing refusals and
`github.checked_pr_url`, **BEFORE `gates.open_gate`**, and before the subprocess:

```
fetch origin <base_ref>                       # branchproof.fetch(repo, base_ref)
tip = branchproof.tip(repo, f"origin/{base_ref}")      # NEW, see below
if not tip or not branchproof.is_ancestor(repo, tip, sha):
    raise StaleBase(...)
```

**New signature:** `apply(store, wo, sha, approval, cwd=None, *, base_ref: str)`.
`base_ref` is REQUIRED keyword-only; the repo is `cwd`, which every real call site already
passes as `project.path` (daemon.py:5264) and which `checked_pr_url` already depends on for
its origin check. `cwd is None` or `base_ref == ""` **refuses** — fail-closed: a merge with
no local checkout cannot prove freshness, and there is exactly one production call site.

**New `branchproof.tip(repo, ref) -> str`**, beside `is_ancestor` (branchproof.py:206):
`git rev-parse --verify <ref>^{commit}`, guarded by `REF_RE`, `""` when git cannot say.
It is needed because `is_ancestor` requires its `ancestor` argument to match `SHA_RE` — a
ref name is refused — and because the BASE TIP is precisely the fact nothing in the OS
currently reads fresh.

**Not `pr.base_oid` from the poll's `gh pr view`. THIS IS THE LOAD-BEARING SENTENCE OF THE
WHOLE SPEC, not a belt-and-braces extra.** §1's root cause is exactly that `pr.base_oid`
lagged the real tip by three commits and 5.3 hours, so every test asked of it answered
about a commit that had not been `main` since breakfast. A freshly-read LOCAL tip is the
only base fact in the OS that cannot lag. The fetch is one network round trip on a path
that is about to make an irreversible write; it is the cheapest thing on this line.

**New exception `StaleBase(AutoMergeRefused)`**, sibling of `MergeFailed` and for
`MergeFailed`'s own reason (automerge.py:148): the daemon's `except AutoMergeRefused` arm
logs at DEBUG and writes nothing, because its commonest instance is the ordinary spent
grant. A stale base is news — it must be recorded and it must trigger a catch-up — so it
needs its own arm. `MergeFailed` says GitHub refused; `StaleBase` says the OS refused
before GitHub heard anything.

**THE GRANT IS NOT SPENT.** The check sits before `gates.open_gate`, so a refusal here
burns none of the single `GRANT_USES` authorisation. Two reasons, and the second is the
decisive one:

1. Nothing was attempted. `open_gate`'s contract is "permission to ATTEMPT the act"; this
   command never reached GitHub.
2. **The refusal can be transient and wrong-ish.** `branchproof.fetch` failing, or
   `is_ancestor` unable to answer, refuses fail-closed — and the base may be perfectly
   fresh. Spending the grant on a three-second network blip would strand a green, correct
   merge behind a fresh Neo round trip.

**What happens to the grant afterwards, stated plainly:** it stays `approved` and usable
until its hour is out, and it is harmless. Its command pins `--match-head-commit <old
sha>`, and once §3.4's catch-up runs the head is a new commit, so that grant can never
land anything again; the fresh request §3.5 files carries the new sha. Until the catch-up
runs, every tick refuses at this same precondition. No loop, no merge.

**Daemon arm** (daemon.py:5264, beside `except automerge.MergeFailed`):

```
except automerge.StaleBase as exc:
    store.add_event(wo_id, ops.MERGE_BASE_STALE_EVENT, {...})   # deduped per (head, base tip)
    self._catch_up_with_base(project, store, wo, pr)
    return
```

Event `ops.MERGE_BASE_STALE_EVENT = "automerge_base_stale"`, payload `head_sha`,
`base`, `base_oid`, `approval_id`, `reason`; deduped per (head_sha, base_oid) with
`ops.rejudged_heads`'s shape (ops.py:4080), because a parked pull request reaches this
every two minutes. `timeline._describe` branch beside `automerge_failed`: label
`"Merge refused — the base moved under it"`, detail `round {n} passed on {head[:10]}, and
{base} is now at {base_oid[:10]}, which that commit does not contain — catching the branch
up instead`.

### 3.2 Part 2 — the same fact as a seventh condition in `decide`

New frozen dataclass beside `BaseRed` (automerge.py:161):

```
@dataclass(frozen=True)
class BaseBehind:
    base: str        # the base ref name
    base_oid: str    # the base's tip as this tick read it
```

New parameter `base_behind: BaseBehind | None = None`, new code
`HELD_BASE_MOVED = "base_moved"` (one code per condition — kn-0aba30f0).

Reason sentence: `` `{base}` has moved to {base_oid[:10]} and this branch does not contain
it, so its CI has never run against the code this merge would land on — catching the branch
up first ``.

**Where in the order: immediately AFTER `base_red`, BEFORE the `validated_head` /
`HELD_SHA_MOVED` checks (conditions 4 and 5).** Two reasons:

* It is a fact about the WORLD, like `base_red`, and both belong on the same shelf.
  `base_red` stays first: a broken `main` stops everything, including a catch-up onto it.
* The existing comment on the ordering is about round numbers. Catching the branch up
  MOVES the head, so any round spent on the present head — `_rejudge_moved_head`, reached
  from the `HELD_SHA_MOVED` arm — would be spent on a commit that is about to be replaced.
  Holding on `base_moved` first is what stops that.

**`only_the_head_moved` (automerge.py:363) calls `decide` too, and it must NOT forward the
new parameter.** It keeps its signature; the parameter defaults to `None` on that path, on
purpose. It asks a counterfactual — "would this merge if a round had judged the head it
has" — and the base's freshness is not a fact about whether the DIFF is worth judging. A
stale base does not make a moved head unworthy of a round, and the catch-up that follows
carries the verdict across for free (predecessor §3). Forwarding it would make a
behind-branch permanently unre-judgeable once `CATCH_UP_MAX` is spent.

In practice the arm never runs on a base-stale tick: §3.4's daemon order returns first.
The default is the guarantee for the tick after the catch-up is exhausted.

The daemon computes the fact where it already computes the other one — in
`Daemon.auto_merge`, before `decide`:

```
base_behind = (automerge.BaseBehind(base=pr.base_ref, base_oid=pr.base_oid)
               if ops.catch_up_needed(pr, repo=project.path) else None)
```

`ops.catch_up_needed` — the ONE home of the rule, `pr.behind` short-circuit and all, plus
the `base_tip` keyword of Amendment B. Cost: it is already paid on the armed path; hoisting
it above `decide` moves the call, it does not add one. It is computed only when
`cfg.enabled and cfg.auto_merge` (the method already returns above that), so an opted-out
project pays nothing.

**THIS ONE READ KEEPS `pr.base_oid` AND FETCHES NOTHING PER TICK** (Amendment B, call site
3). It is a cheap early hold and never the safety property; the safety property is the pair
that FETCHES — the catch-up and the pre-merge precondition — and no merge can reach GitHub
without passing the second.

**SO THE FETCHED READ OVERRIDES IT, AND THAT IS NOT OPTIONAL.** The cheap read can
over-report as well as under-report, persistently: a checkout that does not have
`pr.base_oid` at all cannot answer the ancestry question, and `ops.catch_up_needed`'s
documented rule reads "cannot answer" as BEHIND — every tick, for ever. `pr.behind` against
a freshly-fetched base that IS contained is the milder second way in. So when the catch-up
comes back `CATCH_UP_NOT_NEEDED`, the tick re-runs `decide` with `base_behind=None` (the
carry re-decide's shape) and continues down the normal path instead of returning, logging
at debug what disagreed — `base_oid` and `behind` both named. Returning there would hold a
mergeable pull request for ever and write nothing saying why.

**AND THE CATCH-UP HANGS OFF THIS HOLD.** Because the base fact outranks conditions 4-6,
the armed path can no longer be reached while the branch is behind, so
`Daemon._catch_up_with_base` runs from the `HELD_BASE_MOVED` arm — both ways of being
behind (GitHub's own `BEHIND`, and the ancestry answer it reports CLEAN for) arrive there.
The hold written is §3.4's outcome sentence, not `decide`'s generic one, so the record says
which guard refused. §3.6's supersede runs in the same arm, before the catch-up, for the
same reason.

### 3.3 Part 2b — nothing is ARMED on a stale base either

Because `HELD_BASE_MOVED` precedes conditions 4-6, `decide` cannot return `armed=True`
while the branch is behind. That is the second half of "nothing is proposed or armed":
`propose` is only reached from the armed path, so a stale base now cannot file a gate
request at all. §3.1 remains necessary regardless — `decide` runs on a poll tick and the
merge runs minutes later, which is exactly the #830 window.

### 3.4 Part 3 — a refused catch-up HOLDS, it does not fall through

`Daemon._catch_up_with_base` returns a `PullRequest` today, so its caller cannot tell
"already up to date" from "behind and I could not fix it this tick". Change the return
type.

New frozen dataclass and outcome constants in `ops` (beside `catch_up_needed`, ops.py:5842):

```
CATCH_UP_NOT_NEEDED = "not_needed"   # the base tip is already an ancestor of the head
CATCH_UP_DONE       = "done"         # `ci.update_branch` ran; the head moved
CATCH_UP_DEFERRED   = "deferred"     # a guard said "not this tick" — clears by itself
CATCH_UP_EXHAUSTED  = "exhausted"    # CATCH_UP_MAX / base_heal_spent — will NOT clear
CATCH_UP_FAILED     = "failed"       # gh/git refused; attempt spent, retried next tick

@dataclass(frozen=True)
class CatchUp:
    pr: Any            # the PullRequest to decide against — unchanged unless DONE
    outcome: str
    reason: str        # a sentence for the user, per guard
```

Mapping from today's seven guards (daemon.py:6193 onwards). **The order changes in one
place, and Amendment B is why:** the fetch and the local tip read move ABOVE the two
spending guards, because those guards must be keyed on the fresh tip — and "not needed" is
judged before them, because a branch that is already up to date must never read as
`EXHAUSTED`, the one outcome that never clears on its own.

| guard | outcome |
|---|---|
| no `base_ref` / no `pr_url` | `FAILED` |
| busy worker, queued message, open round | `DEFERRED` |
| `branchproof.fetch` false | `FAILED` |
| `not ops.catch_up_needed(..., base_tip=<the fetched tip>)` | `NOT_NEEDED` |
| `ops.base_heal_spent(store, wo_id, <the fetched tip>)` | `EXHAUSTED` |
| `ops.catch_up_attempts >= ops.CATCH_UP_MAX` | `EXHAUSTED` |
| head re-read moved off the judged commit | `DEFERRED` |
| `ci.update_branch` raised | `FAILED` |
| update ran | `DONE` |

Caller — the `HELD_BASE_MOVED` arm (§3.2) in practice, and the armed path's `head_oid`
comparison (daemon.py:5232) is replaced by the same shape for the ticks that reach it:

```
caught = self._catch_up_with_base(project, store, wo, pr)
if caught.outcome != ops.CATCH_UP_NOT_NEEDED:
    self._note_automerge_held(store, wo_id, automerge.held_base_moved(caught, pr))
    return          # NEVER falls through to propose
```

`automerge.held_base_moved(catch_up, pr) -> Decision` is a tiny renderer in `automerge`
beside `_held`, so the hold code and its sentence stay in the module that owns them.
`_note_automerge_held` already dedupes on (head sha, code, reason text), so the reason
text is what distinguishes the cases and must differ per outcome:

* `DEFERRED` — `` `{base}` has moved to {oid[:10]}; the branch is being caught up as soon
  as {what is in flight} finishes — the OS retries every tick ``
* `EXHAUSTED` — `` `{base}` has moved to {oid[:10]} and the OS has already caught this
  branch up {n} time(s) ({ops.CATCH_UP_MAX} is the cap). It will not try again: merge it
  by hand, or `jarvis wo send` the worker to rebase ``
* `FAILED` — `` `{base}` has moved to {oid[:10]} and the update did not run: {reason}.
  The OS retries next tick ``
* `DONE` — `` caught up with `{base}` at {oid[:10]}; CI is running on the new head ``

The `EXHAUSTED` sentence is the one that has to be unambiguous: it is the only outcome
that never clears on its own, and a user reading "deferred" forever is the bug this part
exists to prevent.

The held path's `BEHIND` call site (daemon.py:5211) takes the same treatment: it already
returns, so only the hold sentence changes.

### 3.5 Part 4 — the base tip is recorded ON the gate

**New column**, through this repo's one migration mechanism —
`project_store.ADDED_COLUMNS["approvals"]` (project_store.py:1326), applied by
`ProjectStore._migrate` (project_store.py:1529) on every store open:

```
"base_oid": "TEXT NOT NULL DEFAULT ''",
```

`''` means "this request records no base", the honest reading of every approval written
before this and of every gate kind that has no base. Same discipline as
`validation_rounds.head_sha`'s default.

`ProjectStore.add_approval` gains `base_oid: str = ""`, written to the column and into the
`gate_requested` event payload only when non-empty (the existing `**({...} if x else {})`
shape).

**Why a column and not only the `automerge_proposed` payload:** the §3.6 sweep asks "has
this pending request's base moved" of every pending `auto_merge` row on every tick, and it
already holds the approval row. A payload field would make that a timeline scan per gate
per tick, and `jarvis gate show` would have to go hunting for the fact it is supposed to
render. The `automerge_proposed` payload gains `base` and `base_oid` as well — the event is
read on its own.

`automerge.propose` takes the `PullRequest` itself (`pr=pr`) and reads `base_ref` and
`base_oid` off it — both already there from the poll (predecessor §5.1) — rather than a
loose pair of strings a caller could pass in the wrong order.

**`automerge._request_question` (automerge.py:506):** the `# What was judged` section's
sentence

> That is the commit at the head right now — the two were compared this tick, and
> `--match-head-commit` makes GitHub refuse the merge if it moves before it runs.

becomes

> That is the commit at the head right now, and it CONTAINS the current tip of `{base}`
> ({base_oid}) — both were read this tick, so the CI evidence below ran against the code
> this merge would land on. `--match-head-commit` makes GitHub refuse the merge if the head
> moves before it runs, and the OS re-reads `{base}` immediately before merging and refuses
> on its own if it has moved since you read this.

That last clause is what makes the reviewer's job honest: the packet states what is true
NOW and names the check that will catch it if it stops being true. Gate 308's reviewer had
neither.

### 3.6 Part 5 — a pending request whose base has moved is SUPERSEDED, never answered

**Where:** `Daemon.auto_merge`, on a tick that already holds a fresh `pr` and has already
computed `base_behind` (§3.2). No new scheduling, no new `gh` call. TWO SITES, because
`HELD_BASE_MOVED` now returns before the approval lookup: the hold arm (where it actually
fires, ahead of the catch-up, looking the request up by the judged sha) and the existing
`approval is not None and approval["status"] != "approved"` branch (daemon.py:5240), which
keeps it for any tick that reaches there.

```
if approval["status"] in ("pending", "awaiting_case") and base_behind is not None:
    reason = (f"`{pr.base_ref}` has moved to {pr.base_oid[:10]} since this request was "
              f"filed against {approval['base_oid'][:10] or 'an unrecorded base'}; the "
              f"CI evidence in it no longer describes what this merge would land on. "
              f"Nothing is authorised and nothing is refused — the OS catches the branch "
              f"up and files a fresh request on the new commit.")
    store.supersede_approval(approval["id"], reason)
    if approval["neo_question_id"]:
        neo_store = NeoStore()
        try:
            neo_store.supersede(approval["neo_question_id"],
                                f"SUPERSEDED — `{pr.base_ref}` moved", reason)
        finally:
            neo_store.close()
    return
```

`ProjectStore.supersede_approval` (project_store.py:4822) lands in
`status='expired', closed_as='superseded', decided_by='os'`, authorises nothing, writes a
`gate_superseded` event, and is a **no-op on anything already decided**. That last property
is what makes the race safe: Neo can answer between the read and the write, in which case
the row stays `approved` and §3.1's precondition is what stops the merge.

**Withdrawing the Neo question: it CAN be done, and if it cannot, nothing breaks.**
`NeoStore.supersede` (src/jarvis/neo_store.py:409) records an answer with
`answered_by='os'`, guarded on `OPEN_Q_STATUSES`, and returns `False` when the question is
already answered. `gates.open_gate` (gates.py:1159) is the precedent — the same pair, for
the same reason (production question 118 went on asking the user to rule on an action that
had already run). A `False` means Neo had already ruled; the approval is then decided and
`supersede_approval` no-ops, which is the consistent pair. The backstop is
`invariants.check_neo_escalations_are_live`, which derives the same fact when nobody made
the call.

**Why the next tick files a FRESH request and this is not a propose/supersede loop.**
Sequence after the supersede: next tick, `decide` holds `HELD_BASE_MOVED` (§3.2) →
`_catch_up_with_base` runs → head moves → CI runs on the new head (`HELD_CHECKS_RUNNING`
for a tick or two) → green → `HELD_SHA_MOVED` → the predecessor's carry rebinds the verdict
with no round spent → armed on the NEW sha → `propose` files a request whose command string
carries that sha.

The loop is bounded three independent ways, and a fast-moving `main` hits all three:

1. `ops.base_heal_spent(store, wo_id, base_oid)` — one update per (pull request, base
   commit), so the same base tip is never chased twice.
2. `ops.CATCH_UP_MAX = 3` (`ops.catch_up_attempts`, counting `cause="behind"` rows only) —
   the total, keyed on nothing that a moving base can reset. Past it, §3.4 returns
   `EXHAUSTED` and the pull request HOLDS with a sentence saying so. No further request is
   proposed, so there is nothing left to supersede.
3. `propose` is idempotent per command string, and the string carries the sha — so each
   caught-up head buys at most one request.

Worst case on a busy `main`: three propose/supersede cycles, then a hold that names the cap
and asks the user. That is the designed ceiling, not an accident.

## 4. Why issue item (4) — serialising merge execution per project — is NOT implemented

Part 1 subsumes it, by construction. The thing serialisation buys is "two approved merges
cannot race onto the same base". But any merge to `main` moves `main`'s tip, and the moment
it does, **every other outstanding approval fails the ancestry check in §3.1** — because
their judged commits cannot contain a commit that did not exist when they were judged. The
second merge refuses with `StaleBase`, catches up, and comes back with a fresh verdict and
a fresh grant.

A per-project merge lock would add a cross-thread mutex (or a store-backed lease with its
own expiry, its own stale-lock recovery and its own way to deadlock a daemon tick) to buy a
property the ancestry check already gives for free — and it would still not cover a merge
made by a HUMAN or by another tool, which the ancestry check does cover, because it asks
GitHub's actual ref rather than the OS's own bookkeeping.

## 5. Tests

Pinned ruling: targeted tests only, CI runs the suite.

**`tests/test_automerge.py`** — owner of `decide`'s pure table and of `apply`:

1. **an approval whose PR head does not contain the current base tip never merges**:
   `apply` raises `StaleBase`, `fake_gh` records no `pr merge` call, and the daemon arm
   calls `_catch_up_with_base` instead (the issue's first required test).
2. **the grant-accounting test:** after a `StaleBase` refusal,
   `store.get_approval(id)["uses"] == 0` and `store.usable_grant(...)` is still live —
   `gates.open_gate` was never reached. Paired with: a `MergeFailed` on the same fixture
   DOES spend it, so the two directions are both pinned.
3. `decide` returns `HELD_BASE_MOVED` with a `BaseBehind`, and returns it **in preference
   to** `HELD_SHA_MOVED`, `HELD_CHECKS_*` and `HELD_MERGE_STATE_UNCLEAN`, and **after**
   `HELD_BASE_RED` — one test per ordering pair, since the order is the design.
4. `only_the_head_moved` still answers True on a behind branch whose only defect is a moved
   head (the parameter is not forwarded — §3.2).
5. the AST test over `WRITE_VERBS` still passes: `apply` gained a `git` call through
   `branchproof` and no new `gh` verb.
6. `apply` with `cwd=None` or `base_ref=""` refuses and merges nothing (fail-closed).

**`tests/test_branch_catch_up.py`** — owner of the catch-up and its guards, and it already
has the `local_proof` helper for faking `branchproof`:

7. **two approved merges in a row: the second is caught up before it merges.** Merge A
   lands and moves the base; B's next tick refuses at §3.1, catches up, re-greens, carries,
   re-proposes and merges — with no round spent (the issue's third required test).
8. **both arms of the ancestry test** — `pr.behind` True with `base_oid` unset, and
   `pr.behind` False with the base tip NOT an ancestor of the head. kn-907c9a61's rule;
   the second arm is the one this repository actually produces.
9. every guard maps to its `CatchUp.outcome` and its distinct hold sentence, and **none of
   them reaches `propose`** — one case per row of §3.4's table. The `EXHAUSTED` rows are
   the regression this whole spec turns on.
10. `branchproof.tip` returns `""` when the ref is unknown and the merge then refuses.

**new `tests/test_stale_base_gate.py`** — part 4 and part 5, their own file because they
are about the APPROVALS ledger rather than about the branch:

11. **a pending request whose base moved is superseded rather than decided**: status
    `expired`, `closed_as='superseded'`, `decided_by='os'`, the linked Neo question answered
    with `answered_by='os'`, and `usable_grant` empty (the issue's second required test).
12. the same sweep is a **no-op** on an already-`approved` row and on an already-answered
    Neo question — the race in §3.6.
13. `approvals.base_oid` survives `_migrate` on a database created without it, and
    `propose` writes the tip it read; the request text names `{base}` and the tip.
14. the bounded loop: a base that moves on every tick produces at most `CATCH_UP_MAX`
    propose/supersede cycles and then one `EXHAUSTED` hold.

**`tests/test_timeline.py`** — 15. `automerge_base_stale` and each new hold sentence render,
and the `EXHAUSTED` one names the cap.

## 6. Scope, and what is deliberately NOT covered

* **Rebase or force-push catch-ups.** `ci.update_branch` merges on purpose (ci.py:209) and
  the predecessor's carry rests on that provenance.
* **A base branch other than the pull request's own `baseRefName`.**
* **Bounding Neo's review latency.** 3.98h to answer gate 308 is a real problem and it is a
  different one; this spec makes the latency SAFE, not short. If the fleet wants it short,
  that is its own order.
* **`GRANT_TTL_SECONDS`.** Unchanged, and §1 says why touching it would have been a
  symptom fix.
* **`CATCH_UP_MAX`.** Unchanged, and now knowably so: no guard fired on #829 (§1), so
  raising the cap would have changed nothing. That follow-up is dead.
* **The gate reviewer's prompt beyond the one sentence in §3.5.** No change to the persona,
  no new escalation rule.
* **Bounding how stale GitHub's own `baseRefOid` may be.** Nothing here asks GitHub to fix
  its cache; the OS stops believing it where the answer matters (Amendment B) and keeps
  reading it where a stale answer is harmless.

## 7. Amendments folded in

**A — the root cause is established.** §1 and §6 said "which guard fired is NOT
established". It is, from wo-672bd388's timeline, and the answer is that NONE did: the
ancestry test answered on `pr.base_oid`, which lagged `main` by three commits and 5.3
hours. §1 is rewritten around that; §3.1's insistence on a freshly-read local tip is the
load-bearing sentence of the spec; `CATCH_UP_MAX` does not want raising.

**B — where the authoritative base tip is read.** `ops.catch_up_needed` gains a keyword
`base_tip: str = ""`. Non-empty is AUTHORITATIVE and `pr.base_oid` is ignored; empty keeps
today's behaviour exactly. `pr.behind` stays a positive short-circuit in both arms
(kn-907c9a61). One home for the rule, both arms testable, and no hidden network call inside
a helper the daemon runs every poll tick. Three call sites:

1. `Daemon._catch_up_with_base` passes `base_tip=branchproof.tip(project.path,
   f"origin/{base}")`, read AFTER the `branchproof.fetch` it already makes — and uses that
   same tip as the `ops.base_heal_spent` key and as the `base_sha` it records. Keying that
   bound on a stale oid is how the guard mis-fires.
2. `automerge.apply`'s execution-time precondition reads the tip itself, after its own
   fetch, exactly as §3.1 specifies.
3. The daemon's pre-`decide` `BaseBehind` keeps `pr.base_oid` and fetches nothing per tick
   — justified in §3.2, WITH the override that section now states: a `CATCH_UP_NOT_NEEDED`
   from the fetched read re-decides without it rather than returning, because the cheap
   read over-reports permanently on a checkout that cannot resolve `pr.base_oid`.

**C — a regression test for the measured incident.**
`tests/test_stale_base_gate.py::test_gate_308_a_head_that_contains_only_githubs_stale_base_is_never_proposed`
reproduces gate 308's shape: the judged head contains the base tip `pr.base_oid` reports,
`origin/main` is really at a descendant the head does not contain, and nothing is proposed
and nothing is merged.

**D — §3.6's supersede may only read a FETCHED base fact.** `Daemon.auto_merge` computes a
cheap `base_behind` from `pr.base_oid` before `decide`. On `CATCH_UP_NOT_NEEDED` the daemon
re-decides AND sets `base_behind = None`, because §3.6's supersede branch reads that same
variable. Left set, it withdraws a HEALTHY pending request on the next tick — on a checkout
where the cheap read over-reports for ever — and the work order livelocks: superseded, re-filed,
superseded again. Only the fetched answer may close a request.

**D, cont. — the hold AND the supersede name a FETCHED tip, never `pr.base_oid`.** `ops.CatchUp` gains
`base_tip: str = ""`, carrying `origin/<base>` as read locally after the fetch;
`Daemon._supersede_stale_request` takes it as a keyword and quotes a commit only when one is
passed. `approval["base_oid"]` is still quoted — a recorded fact about the request, not a cached
read. Quoting `pr.base_oid` would hand the user the very number this spec establishes is
untrustworthy. The hold sentence broke the same rule: `automerge.held_base_moved` took `base_oid`
as the freshly-read tip but fell back to `pr.base_oid` when a caller passed none, and two of its
three daemon call sites passed none — so the user-facing hold quoted the cached read, including a
`CATCH_UP_DONE` sentence claiming the branch had caught up with it. The fallback is gone: no
fetched tip means the sentence names no commit at all, and no placeholder stands in. All three
call sites now pass `ops.CatchUp.base_tip`.

**D, cont. — `gates.GRANT_TTL_SECONDS` is TESTED, not just argued about.** §6 keeps the argument that a TTL
would not have prevented gate 308 — grant spent 4.3h after approval, so "inside the 3600s window"
is false, and the base moved regardless of age. The argument now rests on a test:
`tests/test_automerge.py::test_an_approved_grant_past_its_ttl_merges_nothing` backdates the
grant's `expires_at` and asserts `AutoMergeRefused("no longer a live grant")` with nothing
reaching `fake_gh`.

**E — §3.6's supersede is narrowed to three conditions, all required.** A pending
`auto_merge` request is withdrawn ONLY when: (1) the catch-up actually FETCHED, i.e.
`ops.CatchUp.base_tip` is non-empty; (2) that fetched tip really shows the base moved
(`ops.catch_up_needed(pr, repo=..., base_tip=tip)`); (3) the outcome is not
`CATCH_UP_DEFERRED`. A PRE-fetch refusal — no base branch or pull request recorded,
`branchproof.fetch` failed, a turn, message or round in flight — carries no base fact at
all, so superseding on it withdraws a healthy request on the strength of `pr.base_oid`,
the cached read this spec exists because it cannot be trusted. A deferral is transient: the
guard clears by itself and the next tick re-reads, so withdrawing there costs a fresh
five-seat request and a fresh Neo review for a move that may never need one. That covers
the POST-fetch deferral too — the push that moved the head off the judged commit — which
does carry a tip.

**E, cont. — what did NOT change.** The HOLD still fires for every non-`CATCH_UP_NOT_NEEDED`
outcome, so a refusal stays visible on the timeline; and superseding stays a no-op on
anything already decided.
