# A person can open a validation round, without pretending a worker finished

*Design, 2026-09-15. Built in wo-eefccfa9. Neo decided §4 (question 300).*

*Reading order:
[2026-09-14-validated-auto-merge-design.md](2026-09-14-validated-auto-merge-design.md)
(`head_sha`, `judged_head` and the condition this exists to unblock),
[2026-09-13-two-gates-not-a-chain.md](2026-09-13-two-gates-not-a-chain.md) (why a round
and a status are different questions).*

---

## 1. The problem, measured

jarvis-0.10.0 introduced `validation_rounds.head_sha` and `ProjectStore.set_validation_head`.
Every round judged before that release carries the column default `''` — all 73 rounds in
the production project database. `automerge.decide`'s condition 4 needs a recorded judged
commit, so each of those work orders holds on `sha_unrecorded` for ever: the panel accepted
a commit it cannot name, and nothing re-judges it.

Nothing in the OS opened a fresh round. `jarvis validation` had only `show`, and the only
surface reaching `ops.submit_for_validation` was `ops.finish` — the worker's own
`jarvis wo finish`.

Getting wo-45b377e6 (PR #242) merged meant re-running `jarvis wo finish` on a work order
already parked in `waiting_pr_merge`. It worked. It is wrong three ways, and all three are
about the RECORD rather than the mechanism:

1. it writes a `finished` event, so the timeline claims a worker reported a result when no
   worker ran;
2. `--summary` is required, so the operator authors words that land on the record as the
   worker's account;
3. afterwards it is indistinguishable from a genuine re-delivery — nothing says a human
   forced a re-judgement, or why.

## 2. The command

`jarvis validation force <wo-id> --reason "<why>"`.

**`validation force`, not `wo validate --force`.** The subject is the ROUND, not the work
order: it belongs beside `validation show`, which is the other command about rounds, and
`jarvis wo` verbs are all things that happen to a work order rather than to its
deliberation.

It calls `ops.force_validation`, which reaches `ops.submit_for_validation` DIRECTLY. No
`finished` event is written anywhere on the path — that is the point of the command, not a
side effect of it. The evidence it declares is `ops.declared_evidence`, the worker's own
last `--evidence` recovered from its `finished` payload, so nobody has to author a
worker's account to re-judge one.

## 3. The reason is a column

`validation_rounds.forced_reason`, `NOT NULL DEFAULT ''`, added through `ADDED_COLUMNS`
like `head_sha` and `config_version` before it — the table already ships, so a live
database gets it only there. `''` means "a submission opened this", which is the honest
reading of every round written before the column existed.

It is a column and not only an event because the round is what every surface already
reads: `ops.round_line` renders `· forced: <reason>` on the same line `wo show`, `fo show`
and both dashboard pages print, so a forced round can never be mistaken afterwards for a
worker re-delivering. A `validation_forced` timeline event is written beside it, carrying
the reason and the status the work order was in.

## 4. `max_rounds`: the budget is left alone

The brief's premise — "a work order already at its last round cannot be re-judged at all"
— is **false**, and checking it is what decides this section. Nothing about OPENING a round
consults `cfg.max_rounds`: `submit_for_validation` numbers via `counted_validation_rounds
+ 1` and never reads it. `max_rounds` is a settle-time branch in
`Daemon._validate_work_order`:

- `rejected` and `n < max_rounds` → feedback goes to the worker over the bus;
- `rejected` and `n >= max_rounds` → `_escalate`: the work order goes to `needs_review`,
  the user is flagged and notified.

So a forced round takes the next number like any other, and **a rejection at or past the
budget escalates to the user**. That is the correct landing for a round a person forced:
the motivating population is work orders parked in `waiting_pr_merge` with no worker left
to send feedback to, and a refusal of a re-judgement someone asked for is theirs to read.
The help text says so.

**The alternative was rejected as a hang, not as a preference.**
`counted_validation_rounds` both counts rounds and NUMBERS them. A forced round excluded
from the count would have its number reused by the next submission, hit the idempotent
insert in `open_validation_round`, and hand the caller an already-CLOSED round — while
`submit_for_validation` parked the work order in `validating` with nothing left to settle
it. Exempting the budget honestly needs round-number and round-count decoupled first, and
that is a different change. There is no `--max-rounds` override.

## 5. What it ALLOWS, and what it refuses

**`FORCEABLE_STATUSES = ("waiting_pr_merge", "needs_review")` — an allowlist.** The
question is not "has this work order settled" but "has it DELIVERED, and is nobody
typing". Two different failures sit either side of it:

- a **settled** order (`completed`, `cancelled`, `failed`) would be *reopened* into
  `validating` by a verdict that could change nothing;
- a **live** one (`running`, `dispatching`, `waiting_input`) has a worker still writing to
  the branch — and an open round OWNS that worker's session (kn-01a4ab27), so
  `Daemon._reject` would post the panel's feedback into a session mid-task. That is the
  two-writers bug that knowledge entry exists to prevent, arriving by a new door.
  `pending` has not begun and has nothing to judge.

An allowlist rather than a `running`-shaped refusal, so a status added to `WO_STATUSES`
tomorrow is refused until somebody decides it is safe — not allowed until somebody
remembers it is not. `validating` is absent for its own reason: its round is open by
definition, which is the next row's refusal with its own sentence.

The rest:

| Refused | Why |
|---|---|
| blank `--reason` | the reason is what makes the round legible as forced; a blank one gives back the defect |
| the work order does not exist | `find_work_order` — covers a deleted order |
| no `pr_url` | a fresh round would read the worktree and record `''` — exactly the state this exists to escape |
| a round the machine still owns | `ProjectStore.round_machine_owns` off the latest round (`pending`/`failed`): a second round underneath takes the MAX-round slot from the one about to run |
| `validation.enabled` off for the project | the submission sites are the only place that switch is read (`ops.finish`), and this is a new submission site |

### 5.1 `round_machine_owns` and the latest-round rule

The refusal above names the round it refuses over, so it needs the predicate AND the row.
Reading twice is the kn-08f2ff9b shape — the panel opens rounds on another thread, and two
reads can straddle one. So `ProjectStore.round_machine_owns(round_row)` is a staticmethod
over the row (it *cannot* re-fetch), and `validation_round_open` — which
`Daemon.heal_pull_request` depends on — is reimplemented on top of it.

**The rewrite's risk is the latest-round rule**, not the outcome set. The old SQL keyed on
`round = (SELECT MAX(round) …)`; the new path takes `latest_validation_round`, which orders
`BY round DESC LIMIT 1`. Those agree, and an implementation that reached for "most recently
inserted" instead would agree too on every work order whose rounds were inserted in order —
which is every work order the ordinary path produces. So the test builds rounds **out of
insertion order**, both directions, where latest-by-`id` and `MAX(round)` disagree.

## 6. Where the work order lands

`validating`, and then wherever the panel's verdict puts it. On a pass,
`ops.land_when_cleared` re-parks it in `waiting_pr_merge` behind the still-open pull
request, and `Daemon.poll_pull_requests` can now arm the automatic merge because condition
4 finally has a commit to compare. On an escalation, `needs_review` with the flag.
