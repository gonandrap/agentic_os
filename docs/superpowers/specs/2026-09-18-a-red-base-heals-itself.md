# A pull request red only because its base was red heals itself

*wo-fdfa51c7, 2026-09-18. The recogniser's signal is Neo's, on question 435; the verdict
carry-forward is Neo's, on question 438. Extends
[2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md](2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md)
— read that first; this is a step placed BEFORE its nudge, not a replacement for it.*

## 1. The problem, measured

Two work orders burned three worker turns each on a CI failure no worker could ever fix,
and then stopped dead with an attention item.

| | wo-43c4c665 | wo-b2f88616 |
|---|---|---|
| pull request | #280 | #298 |
| failing check | `unit (3.12)` | `unit (3.11)` |
| run | 35302313324, started 03:12:17Z | 35315816246, started 06:39:22Z |

Both failures were byte-identical to `main`'s own at that moment: run 35298519923 at
9dd7bcc, started 02:13:51Z, the same `TypeError` at
`tests/test_validation_follow_ups.py:827`. **CI builds a pull request as a merge with its
base**, so while `main`'s head was red every pull request in the fleet built red, and no
push to any branch could turn any of them green.

`main` recovered at 15:16:03Z (run 35361364013, e080156). Neither pull request healed:
GitHub does not rebuild a pull request's merge ref when its base moves, so the stale
`FAILURE` conclusion sits there for ever.

What the OS did instead: `Daemon.heal_pull_request` nudged the worker through
`ops.PR_CHECKS` until the attempt budget ran out. The worker read the run, concluded
correctly that the failure was inherited, and said so — three times, identically. About
USD 5.22 across 19 API calls on wo-43c4c665 alone. The supervisor raised both "Effort
being re-spent" and "Blocked on nothing": **the OS diagnosed this correctly and had no
action available but to tell the user.** That gap is what this closes.

## 2. Recognising it: temporal and workflow-scoped, never textual

A failing check is an inherited failure when both hold:

* the base run that was **head when the check started** concluded red, and
* the base's newest **completed** run of the **same workflow** is green now.

`ci.inherited` is that predicate, pure. Three things about it are decisions rather than
implementation:

**Head-when-it-started, not head-when-it-finished.** A pull request built at *t* was
built as a merge with whatever commit was head at *t*, so the verdict that commit
eventually received is the verdict describing the base it inherited — whether or not the
base's own run had finished by then. `ci.head_when` takes the newest base run that had
*started* by *t* for exactly this reason.

**Workflow, not job name** (Neo 435). Matching the failure text looks like the stronger
signal and is the weaker one to build on. The temporal claim is *causal*: GitHub builds
the merge, so a broken base is present in the build by construction. A text match is
circumstantial and costs a log download per red check per tick. Matching the **job** name
is worse than either — fail-fast cancels siblings, so the base and the pull request
routinely name different shards of one matrix. That is not hypothetical: it is shape 2
above, where `main` failed one shard and PR #298 surfaced `unit (3.11)`. A job-name match
would refuse to heal half the population this exists for.

The asymmetry that makes this safe: a **false positive** costs one branch update, bounded
by §3. A **false negative** costs exactly today's behaviour — the worker is nudged.

**Where the network is.** `poll_pull_requests` already talks to `gh`; `invariants.py` must
not. So the recogniser's reads live in the poll, and everything the status line needs is
written down for `invariants.base_red_note` to read back (§4).

`ci.RED_RUN_CONCLUSIONS` is deliberately not `github.RED_CONCLUSIONS` applied to a
different shape: `cancelled` on a base run means a human stopped it, which says nothing
about the code, and reading it as a broken base would rebuild the whole fleet for nothing.

## 3. Healing it: update the branch. A re-run cannot work

**The heal is `gh pr update-branch`, and a re-run is measured not to work.** A re-run
replays the same merge commit — run 35302313324 is fixed at 07a05eb, the merge with a red
`main` — so it reproduces the inherited failure for ever at full CI cost. Both re-runs
tried by hand on 2026-09-18 came back red with the identical `TypeError`. Only updating
the branch regenerates the merge ref against the base as it is now.

Merge, never `--rebase`: a rebase rewrites history, force-pushes, and destroys the
provenance §5 rests on.

**Not a gated action**, decided on the real command rather than on the re-run it replaces.
It does push, which the earlier framing did not, and it is still not privileged: it merges
nothing into the default branch, ships no release, restarts no service, adds no authored
content, rewrites no history, and GitHub **refuses it outright on conflict** rather than
letting anything resolve one — so the worst case is the branch it was already going to be.
Contrast `gate_rules.AUTO_MERGE`, which the OS files for itself precisely because merging
is irreversible and lands code on `main`. The heal never merges: the automatic merge that
may follow still files its own gate, so this changes what that request says and never
whether one happens.

**The write lives in a new module.** `github.py`'s read-only property is load-bearing
rather than tidy — Neo (251) put the panel's fetch there so that "a judging seat cannot
write to GitHub" is a property of the code, and an AST test proves it. So `ci.py` carries
the one write, under its own allowlist and its own AST test. The OS's whole GitHub write
surface is now two commands in two modules.

**The bound is one update per (pull request, base sha)** — `ops.base_heal_spent`, keyed
rather than counted. If the merge ref has been rebuilt on a green base and CI is still
red, the failure is the branch's own and the existing worker nudge is the correct next
step; updating again would only produce the same commit, which is the re-run trap one
level along. A later base recovery is a different sha and earns a fresh attempt, because
it is a fresh question. **A refusal spends the attempt too**, or a pull request GitHub
will not update is retried every two minutes for ever and never reaches the worker.

**A conflicting pull request is answered structurally, not by policy.** `elif
pr.conflicting` precedes `elif pr.failing` in the poll, so one GitHub says will not merge
never reaches this heal at all. `ops.PR_CONFLICT` owns it — the right owner, since a
worker resolving a conflict merges its base in anyway, which cures the inherited failure
too.

**Two guards, and they are `heal_pull_request`'s minus the ones about messages.** No
session and a queued message do not matter here — nothing is being said to anybody — but a
turn in flight and an open validation round do, and for a stronger reason: this moves the
branch head. Under a running worker that is a push it did not make; under an open round it
is the branch moving beneath the seats mid-judgement, the hazard Neo 283 ruled on.

## 4. Not spending a worker turn on the unfixable case

**While the base is red, nudging is pure waste** and the OS holds instead: no attempt
spent, no message queued. `Daemon.heal_inherited_failure` returns `handled` and the poll
never reaches `heal_pull_request`.

Silence would be its own defect — a parked work order with a red pull request and no nudge
looks exactly like one the OS forgot. So the hold is said on the status line:

```
waiting_pr_merge — waiting for `main` to go green — the base's own build is red,
                   so nothing this branch pushes can pass
```

It is **not** an attention item and `true_blockers` does not derive it: nobody owes a
decision, and the OS is waiting for a build that is already running.

`invariants.base_red_note` renders it in **one indexed read**, and the write side is what
buys that: `pr_base_health` is recorded only on a *transition*, so the newest row is the
current answer and no second kind has to be read to know whether it is stale.

**Making the recovery observable** (the other half of the user's rule — "it fixed itself"
must be a fact on the record): `pr_base_updated` carries the base, the base sha, the head
before and the head after, following how `clear_pr_repair` logs a healed episode.

**What it costs.** The poll's green case goes from three indexed reads to four — the
fourth takes the waiting note down, and is paid *inside* `checks_green`, so a red or
queued pull request never pays it. It was bought deliberately: a pull request that goes
green while its base is still broken would otherwise keep a status line pointing at a
build that no longer blocks it. `tests/test_pr_checks.py` counts the statements, so a
fifth fails a test. The `gh run list` is **one per project per tick**, filled lazily, and
never made at all while the fleet is green.

## 5. The heal is done when the pull request MERGES, not when CI goes green

**Updating the branch moves the head, and `automerge.decide` refuses a commit no round
judged.** So the naive heal turns a red pull request into a green one held `sha_moved` —
the same stall wearing a better label. Both production pull requests did exactly this when
the user updated them by hand:

* PR #298 — judged a650fd2c98, head now 85dbac55fb, every check green, `CLEAN`, and it did
  not merge.
* PR #280 — judged 709582ae53, head now c2120424ba. Same shape.

Both needed `jarvis validation force` by hand. A self-heal that updates branches
automatically would generate this **every single time** rather than occasionally.

**So the verdict is carried forward, not re-taken** (Neo 438). The alternative — opening a
fresh round — is correct and wrong in practice: it spends a round number, so a pull
request near `max_rounds` is pushed past it by a heal it never asked for, manufacturing an
attention item out of the self-heal; and it costs a five-seat panel per healed pull
request, fleet-wide, every time `main` goes red then green.

**A SECOND COLUMN, NEVER A REBIND.** `head_sha` means "the commit the seats read" and must
stay true on `jarvis validation show` for ever — it is also the evidence that the carry was
legitimate. `carried_head_sha` is written beside it, and `ProjectStore.validated_head`
prefers it.

`ops.carry_validated_head` writes it only when all three hold:

1. the latest round **passed** and the commit it accepted is `judged`;
2. `judged` was the head the OS updated **from** — re-read *after* the guards and
   immediately before the update, so a worker push that beat us means nothing is carried;
3. `head_after` is a **two-parent merge whose first parent is `judged`**, proved by
   reading the commit back from GitHub (`ci.commit_parents`).

Together these say the difference between the judged commit and the new one is a merge of
the base and nothing else. Any later push moves the head off `carried_head_sha` and
`decide` holds `sha_moved` again — correctly, because then there *is* new authored content.

**Fact 3 is not belt and braces; it is the only one that cannot be raced** (review round
1). `gh pr merge` pins its commit server-side with `--match-head-commit`, so the automatic
merge can make GitHub refuse if the head moved. **`gh pr update-branch` has no such
flag** — confirmed against the CLI, its only option is `--rebase`. So between reading the
head and the update landing there is a window in which a worker turn can end and push, and
fact 2 narrows that window without closing it. Fact 3 closes it by asking GitHub what the
resulting commit *actually* merged: a head built on a worker's push has that push as its
first parent, not `judged`.

First parent, not "one of the parents" — the first is the side the merge was made onto, so
a commit taking `judged` as its *second* parent is a different history with somebody
else's work at its root. Exactly two, because a rebuilt merge ref has the old head and the
base and nothing else.

The failure this guards against is silent and falls open. Without it the OS would bind the
panel's verdict to code the seats never read and record "no authored content changed"
beside it — a false justification that `automerge.decide` and the `AUTO_MERGE` gate request
would both then repeat to Neo. An unprovable carry is therefore not a carry: if the parents
cannot be read, the pull request stays bound to the commit the seats judged and waits for a
person.

**What is not relaxed:** CI must still pass on the carried commit (condition 6), a round
that recorded no judged commit can never be carried onto one, and the merge still files
its `AUTO_MERGE` gate for Neo.

## 6. The state machine

The heal is not one call. Per parked pull request, per tick:

```
  base red?  ──yes──▶  HOLD: no attempt, say so on the status line
     │ no
  inherited? ──no───▶  FALL THROUGH: the worker nudge, exactly as today
     │ yes
  spent for this base sha? ──yes──▶ FALL THROUGH
     │ no
  re-read the head (fact 2) ──▶ update the branch ──refused──▶ spend it, FALL THROUGH
     │
  re-read the head → record `pr_base_updated`
     │
  read the new commit's parents ──not a merge onto `judged`──▶ no carry; it waits
     │
  carry the verdict (§5)
     │
  (next tick) CI green on the new head → the existing AUTO_MERGE gate → merge
```

## 7. The fleet, which is the same gap one level up

When `main` goes from red to green, **every** open pull request in the project is carrying
a stale red check — not just the ones a worker happened to be nudged about. A heal driven
per pull request by something that was already looking at that pull request would leave
the rest sitting red until somebody noticed.

So the base's CI is read **once per project per tick** and shared across the whole loop.
One `gh run list`, every parked pull request rebuilt on the same tick, including ones with
no repair episode and no session to nudge — `heal_pull_request` returns early without a
session, so those could never be repaired at all before this.

Pull requests with no work order are out of scope: the OS has no record to heal.
