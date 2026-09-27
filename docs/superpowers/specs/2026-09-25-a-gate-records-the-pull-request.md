# A gate records the pull request

GitHub issue #742. Work order wo-8d964c56. No open decisions: Neo ruled the design on
2026-09-25 (option B, `pr_state` untouched, the merge recorded as a `pr_merged` event).

## The problem

`work_orders.pr_url` has exactly one writer: `ops.finish` (`src/jarvis/ops.py:3967`,
`fields["pr_url"] = pr_url`), reached only from `jarvis wo finish --pr` (`cli.py:2386`).
Every other settlement route writes nothing there.

A planner never takes that route. `ops.submit_plan` (`ops.py:5690`) is the planner's
terminal action and settles it at `ops.py:5795` with
`finish(fo["plan_wo_id"], "submitted a plan for …")` — positional summary, no `pr_url`.

Measured shape, wo-83e4183c (planner of fo-ff8570fa): PR #735 opened, `pr_merge` gate
filed for `gh pr merge 735 --squash`, gate 226 approved by Neo, the merge ran, the work
order reached `completed` with `pr_url: None` / `pr_state: None`. The pull request number
is in the record — inside `approvals.command` — and nothing reads it.

What that costs, reader by reader:

* `invariants.check_pull_request_recorded` (INV-PR-RECORDED, `invariants.py:2803-2826`)
  skips on `if wo.get("pr_url"): continue`, so an order like this is a violation whenever
  its settlement event recorded commits, with no remedy but typing one by hand.
* `Daemon.refresh_landings` (`daemon.py:4499-4503`) filters candidates on
  `wo.get("pr_url")`, so INV-WORK-LANDED is structurally silent about it — and "silent" is
  not "landed" (`landing.py:71`).
* `Daemon.poll_pull_requests` (`daemon.py:4306`) never sees it.
* `issues.py:704-705` omits the `Landed in <url>.` line when it closes the tracker issue.
* `supervisor.py:450` reports `no PR` into the health judgement.
* `ops.os_status` (`ops.py:593`), `jarvis wo list` (`cli.py:2287`) and the dashboard show
  no link.

Two further facts that shaped the fix, both verified:

* `ops.unlanded_work` (`ops.py:2902`) did not catch this order. It reads the WORKTREE; the
  planner's commits had already merged, so nothing was ahead of base and `finish` did not
  refuse. Issue #232's guard is worktree-shaped and cannot see a merged pull request that
  was never recorded.
* `automerge.propose` (`automerge.py:520`) builds its command FROM `wo["pr_url"]`, so an
  `auto_merge` gate can only exist where the column is already populated. **Only the
  `pr_merge` kind can ever supply a URL the record does not already have.**

Correction to a fact this work order was briefed with: `Daemon.poll_pull_requests` does
**not** select `waiting_pr_merge` alone. It selects `PR_POLL_STATUSES =
PR_REPAIR_STATUSES + ("validating",)` (`daemon.py:138`; `PR_REPAIR_STATUSES` is
`waiting_pr_merge`, `needs_review`, `waiting_input`, `failed` at `invariants.py:202`).
`completed` is not among them, which is what makes the poll and the landing sweep
disjoint — see §5.

### Root cause, stated plainly

`pr_url` is modelled as a SUBMITTER DECLARATION (`github.py:103`, `landing.py:58`), and
one settlement route — the planner's — has no submitter that ever declares. This spec does
not change that model; it adds a second, verified source for the same column and keeps the
declaration as the only thing that ROUTES. The root cause that stays open: `submit_plan`
calls `finish` with no way to pass a pull request.

## The fix

### 1. The parser — `gates.pull_request_in(command, cwd)`

New function in `gates.py`, beside `classify`. Returns a checked URL or `""`. Pure except
for `github.origin_repo`, a local `git remote get-url` with no network
(`github.py:161-179`).

Every shape `gate_rules` recognises for the merge kinds
(`SEED_MATCHES`/`SEED_CANARIES`, `gate_rules.py:1289-1314`), and what is resolvable
offline:

| command shape | resolvable without network |
|---|---|
| `gh pr merge <https://host/o/r/pull/N> …` | yes — the URL is literal |
| `gh pr merge 735 --squash` | yes — number plus `origin_repo(cwd)` builds the URL |
| `gh api --method PUT repos/o/r/pulls/735/merge` | yes — owner, repo and number are all in the string |
| `gh pr merge <branch> …` | **no** — branch to pull request needs `gh pr view` |
| `gh pr merge --auto` with no positional | **no** — the current branch, needs a round trip |
| `automerge.merge_command` output (`gh pr merge <url> --squash --match-head-commit <sha>`) | yes, and redundant: built from `pr_url` |

The two unresolvable shapes return `""` and record nothing. **No network call is added on
any path in this spec.** A `gh` round trip inside a verdict handler would make a gate
decision depend on GitHub being reachable; the daemon already has two bounded pollers that
ask about a URL once one exists.

### 2. Where the security check lands

A gate command is WORKER-WRITTEN TEXT. kn-c748e0fe and `github.UntrustedPullRequest`
(`github.py:100-119`) both apply: a string that is not `PR_URL_RE`-shaped can be read by
`gh` as a FLAG, and a well-formed URL on another repository is a fetch of a stranger's
pull request with the operator's credentials — later presented to the validation panel as
this order's artifact (`evidence.py:495-502`, `validation.py:485-495`).

`pull_request_in` returns **only** what `github.checked_pr_url(url, cwd=project_path)`
returned, inside the parser, before the string is written or passed anywhere. That
includes the `repos/o/r/pulls/N/merge` form, whose owner and repo are worker-supplied text
and are compared against `origin` like any other. `UntrustedPullRequest` is caught and
read as "no pull request": refusing to record is the safe direction, and `checked_pr_url`
already SKIPS rather than fails the origin comparison when `origin` is unreadable.

### 3. Where the column is written — `gates.apply_decision`

`gates.apply_decision` (`gates.py:1279`) is the single choke point for both decision
routes: `Daemon._deliver_gate_verdict` (`daemon.py:3223`, inside the daemon, `pstore`
passed in) and `ops.decide_gate` (`ops.py:6693`, inside a `jarvis` CLI process with its
own `ProjectStore(path)`). It already holds the store — hence `store.project_path` for
`cwd` — and already branches per kind.

Write when ALL of:

1. the verdict recorded is `approved` — never `dismissed` (a dismissal asserts the command
   performed no privileged action, so it is not evidence of a merge) and never `denied`;
2. `approval["kind"] in (PR_MERGE, AUTO_MERGE)` — `auto_merge` is listed only so a future
   caller cannot regress it; today it cannot reach this branch with an empty column;
3. `pull_request_in` returned a URL;
4. the order's `pr_url` is empty.

Emit one timeline event, `pr_url_recorded {approval_id, pr_url, source: "gate"}`, so the
record says where the column came from — `finish` writes `finished {pr_url}` and the two
must never be confusable, which is also what §4's predicate reads. The whole block is
wrapped so nothing can escape: a verdict that fails to record a URL must still be a
verdict.

Rejected sites, each with the reason it loses:

* **`gates.file_request` (`gates.py:895`; callers `hooks._resolve_gate` at `hooks.py:669`
  in the worker's PreToolUse hook, `ops.request_gate_approval` at `ops.py:6541`,
  `ops.contest_gate_match` at `ops.py:6628` — all with a store open).** Filing means a
  worker ATTEMPTED a merge. Recording there puts a pull request on the record for a
  command that may be denied.
* **`gates.open_gate` (`gates.py:1091`; callers `hooks._resolve_gate` at `hooks.py:618`
  in the hook, `automerge.apply` at `automerge.py:706`, `remedies.apply` at
  `remedies.py:591`).** Truer — this is the moment the command is authorised to run — but
  the hook path fails closed (`hooks.gate_decision`'s `except` denies), so an
  `UntrustedPullRequest` or a slow `git remote` there would DENY a command Neo approved.
  It also opens on `dismissed` grants, which are not merges.
* **A discovery sweep that asks GitHub which pull request a branch has.** One network call
  per settled order, already filed as `bl-2aabaee8` and explicitly out of scope of
  INV-PR-RECORDED (`invariants.py:2788-2795`).

### 4. Routing stays declaration-only — through ONE predicate

A URL recorded by a gate is **deliberately not a declaration**, and review round 1 found
that shipped at one site only: `ops.land_finished` asked
`declared_pull_request`, while `Daemon.poll_pull_requests` and the reconciler's park read
the raw column and settled a planner anyway. So the rule is one function, and every router
calls it:

```
def routes_on_pull_request(store, wo) -> bool:     # ops.py, beside declared_pull_request
```

True when `wo["pr_url"]` is non-empty AND the column is not GATE-ONLY. Gate-only means a
`pr_url_recorded` event with `declared_pull_request(store, wo)` empty.

**It is a NEGATIVE test, not "require a declaration".** A row with a `pr_url` and no event
about it — every record written before this change, and the fixtures that set the column
directly — routes exactly as it did. Requiring a declaration would empty the merge queue
of the whole existing fleet.

`declared_pull_request` is unchanged and still the thing that decides: it returns the URL
only when a `finished` event carries one. So a planner whose gate was decided before
`submit_plan` still lands `completed`, and no validation round opens over its merged pull
request — `ops.submit_for_validation` passes `pr_url=wo.get("pr_url")` onto the round
(`ops.py:3272`) and `evidence.collect_work_order` (`evidence.py:495-502`) would fetch it as
the artifact, which nothing excludes a `kind=planner` order from.

The three routers, and nothing else:

1. `ops.land_finished` — on the CALLER'S OWN `wo` copy, never a re-read:
   `review_work_order` hands its landing a copy with `pr_url` deliberately blanked.
2. `Daemon.settle_work_order`'s park (`daemon.py:4066`). Its `else` branch goes through
   `land_finished`, so a gate-only order lands `completed` there.
3. `Daemon.poll_pull_requests`. **And it stays cheap.** The shipped read budget is per
   pull request (`tests/test_pr_checks.py`), so the exclusion is ONE bulk statement per
   STEP — a new `ProjectStore.work_orders_with_event(kind, wo_ids)`, one
   `SELECT DISTINCT wo_id FROM wo_events WHERE kind=? AND wo_id IN (…)`, empty set for an
   empty list without touching the connection. Only the ids that query names pay
   `routes_on_pull_request`. Arithmetic asserted on a step with TWO parked orders:
   `4 * pull requests + 1` `wo_events` statements.

**That predicate is the one line to revisit if another settlement route ever wants the
merge queue.** It is `finished`-event-only today because that is the one event every route
through `ops.finish` writes and no other writer can forge.

#### Every other reader of the column, audited

* `Daemon.refresh_landings` (`daemon.py:4501`) **must keep reading the raw column.** It
  writes `landing_seen` and `pr_merged` and routes nothing, and INV-WORK-LANDED being
  structurally silent about a gate-recorded pull request is the #742 defect itself. Pinned
  by `test_the_sweep_reads_the_pull_request_a_gate_recorded`.
* `ops._awaiting_merge` (`ops.py:4845`) still reads the raw column, and both callers are
  covered by the routers above rather than by a second exclusion.
  `ops._land_after_acceptance` (`ops.py:5122`) uses it only to decide whether to BLANK the
  copy it hands to `land_finished`, which is a router and refuses.
  `ops.resettle_after_repair` (`ops.py:4589`) additionally requires
  `repaired_since_finish` — a repair episode, written only by `Daemon.heal_pull_request`
  inside the poll — so with the poll excluding gate-only orders it is unreachable for one,
  and so is INV-REPAIR-RESETTLED, which is its only other caller.
* `Daemon.sync_issues` (`daemon.py:5818`) CAN reach `ensure_release` with a gate-only
  column: an order that merged through a gate and finished without `--pr` reaches
  `completed`, `issues.desired_state` answers CLOSED, and a `critical`/`blocker`
  `issue_priority` then cuts a release. Cutting a release is the largest move the column
  makes, so it is excluded with the same predicate. `issues.closing_comment` is NOT: the
  `Landed in <url>.` line reading the gate's URL is one of the things recording it was for.

### 5. `pr_state` gains no writer; the sweep records the merge as an event

`pr_state` is stale by construction and `ops._land_after_acceptance` is its only permitted
reader (kn-dbc4971d). **Nothing in this spec writes it.** Its writers stay
`ops.complete_merged` (`ops.py:4196`) and `ops.record_pr_closed` (`ops.py:4220`).

`Daemon.refresh_landings` (`daemon.py:4461-4525`) keeps writing its `landing_seen` event
exactly as today, and gains one conditional write after it:

* when the state just read is `MERGED`, **and** the order has no `pr_merged` event yet
  (`store.events_of_kind(wo_id, "pr_merged")` empty),
* write `store.add_event(wo_id, "pr_merged", {pr_url, head_oid, merged_at,
  source: "landing_sweep"})` — `github.pr_view` already returns `head_oid` and
  `merged_at` (`PR_FIELDS`, `github.py:200`).

**It must not call `ops.complete_merged`.** That function writes `pr_state`, runs
`close_out`, stops a worker and marks the backlog done; every order this sweep reads is
already `completed`. The event is written bare.

Idempotent by that guard, and the guard matters twice: no reader may see two, and
`timeline.build_timeline` has no label for `pr_merged` (it renders through the generic
path), so a second event would print a second merge line on `jarvis wo show` for ever.

The two populations are disjoint, which is why no ordering problem exists:
`poll_pull_requests` reads `PR_POLL_STATUSES` (never `completed`), `refresh_landings`
reads `completed` only. An order that merged while parked gets its `pr_merged` from
`complete_merged`, reaches `completed`, and is then skipped here by the same guard.

### 6. The second writer, audited reader by reader

`ops.complete_merged` is `pr_merged`'s only writer today and its event means *this merge
is what ended the order*. The new one means *this merge was observed after the order
settled*. Every existing reader:

| reader | does the new event change its answer? |
|---|---|
| `ops._overtaken` (`ops.py:2473`) — counts a planner's children carrying `pr_merged`, to tell a planner its plan has been overtaken | **Yes, and correctly.** A child that merged but reached `completed` by another route was invisible; it now counts. The docstring at `ops.py:2466` claims `complete_merged` is the single writer and MUST be corrected in the same change, or it becomes false. |
| `ops.unmerged_pull_request` (`ops.py:2976`) — `""` when a `pr_merged` exists, so `jarvis wo done` writes no `work_unlanded` | **Yes, and no new outcome.** It already treats a settled `landing_seen` as the same signal (`ops.py:2978-2982`), so the new event only makes the first branch fire where the second already did. |
| `ProjectStore.work_unlanded_open` (`project_store.py:3264-3270`) — a park episode is answered by `finished`, `abandoned` or `pr_merged` | **Yes, and this is an INTENDED consequence rather than a side effect.** `land_finished` parks only when `pr_url` is empty, so the sweep can reach a parked order only once §3 has recorded a URL for it — i.e. exactly the #742 order. Its park is then answered by the merge that really happened, and `invariants.true_blockers` stops re-deriving `UNLANDED_BLOCKER` for it — asserted by `test_the_merge_the_sweep_saw_answers_a_park_over_the_same_work`. |
| `Daemon.poll_pull_requests` once-per-closure derivation (`daemon.py:4352-4360`) | **No.** It reads `pr_closed`, not `pr_merged`, and never runs over `completed`. |
| `invariants.check_work_lands` / INV-LANDING-AUDIT-FRESH (`invariants.py:2712`) | **No.** Both read `landing_seen` only, which this path writes exactly as before. |
| `timeline.build_timeline` | Renders through the generic path either way. §5's idempotence guard is what keeps it to one line. |

The distinguishing field is `source: "landing_sweep"`. **No reader keys on it today and
none should start keying on its ABSENCE** — every `pr_merged` row written before this
change has no `source` at all, so absence means "old", not "complete_merged".

### 7. Rendering — one derivation, two surfaces

The merged state on `jarvis wo show` comes from the event, never from `pr_state`. One
helper, on the exact precedent of `ops.automerge_state` (`ops.py:2237`): **`ops.merge_state
(store, wo) -> dict | None`** — `None` for an order with no `pr_url` and no `pr_merged`
event, so a work order the mechanism never touched gains no line. It reads the newest
`pr_merged` event (and, failing that, the newest `landing_seen`) and returns the URL, the
state, `head_oid` and the timestamp.

Both surfaces must call it, and they are the two `automerge_state` already pairs:

* the CLI `wo show` detail dict, `cli.py:2344-2345`'s block;
* the dashboard work-order page, `ui/app.py:999`'s block.

Named together here because the issue's Expected names both, and because a derivation
duplicated in those two files is how they drift.

## Out of scope

* **A planner that opens a pull request and files no gate stays unrecorded.** A gate is
  the only evidence this fix has. The remedy is `submit_plan` learning to pass a pull
  request to `finish`, which is a change to the planner contract.
* **The stale `pr_url`** — the `wo-cd73c537` shape, a column naming the wrong pull request
  (`invariants.py:2788-2795`, `bl-2aabaee8`). §3 only ever writes an EMPTY column.
* **`pr_state`.** No new writer, no new reader.

## Tests to extend, not duplicate

1. `tests/test_gates.py` — classification and the decision paths
   (`test_a_decided_gate_puts_the_work_order_back_to_running`,
   `test_a_dismissed_gate_asks_nothing_of_the_user`,
   `test_deciding_a_gate_does_not_reopen_a_settled_work_order`). §1's shape table goes
   here as a parametrised unit, including both unresolvable shapes, a wrong-origin URL and
   a URL that would be read as a `gh` flag.
2. `tests/test_gates_pipeline.py` — the loop through the real daemon and Neo drain
   (`test_user_can_open_an_escalated_gate`,
   `test_a_decided_gate_cannot_be_decided_again`). The end-to-end "gate approved, `pr_url`
   recorded, `pr_url_recorded` event written" assertion belongs here, plus "a dismissal
   records nothing".
3. `tests/test_feature_orders.py::test_submitting_a_plan_parks_it_for_review_and_settles_the_planner`
   — §4's guarantee: add the case where the gate is decided BEFORE `submit_plan` and
   assert `completed`, no validation round, and a populated `pr_url`.
4. `tests/test_pr_recorded.py` — INV-PR-RECORDED's suite
   (`test_the_invariant_is_silent_when_the_pull_request_is_recorded`,
   `test_the_invariant_reports_a_settled_order_that_wrote_code_with_no_pull_request`, and
   `test_the_invariant_costs_no_subprocess`, which pins "no subprocess" — keep
   `origin_repo` out of the invariant path).
5. §4's ROUTING, one test per router, because round 1 shipped the rule at one site only:
   `test_pr_checks.py::test_a_gate_recorded_pull_request_is_never_polled_and_never_settles_the_order`
   (with its pairing, a declaration that arrives after the gate and must still poll),
   `test_pr_recorded.py::test_the_reconciler_never_parks_a_gate_recorded_pull_request_in_the_merge_queue`
   and `test_issue_lifecycle.py::test_a_gate_recorded_pull_request_closes_the_issue_and_ships_nothing`.
   The read budget is re-asserted in the same file, plus one case over TWO parked orders so
   "per step" is arithmetic rather than a claim.
6. `tests/test_wo_pr_merge.py` and
   `tests/test_pr_checks.py::test_a_work_order_with_no_pr_url_is_never_polled` — the merge
   queue. Both stay green UNCHANGED, which is itself §4's assertion. §5 and §6 need new
   cases beside the landing-sweep tests: the sweep writes `pr_merged` once and never
   twice, it does not settle or re-close the order, and an order already carrying
   `complete_merged`'s event gains no second one.

Existing pins to reference rather than re-prove:
`tests/test_evidence_pull_request.py::test_a_pr_url_that_would_be_read_as_a_flag_never_reaches_gh`
(`checked_pr_url`), and `tests/test_gate_rules.py`'s canary corpus (the command shapes §1
parses are the same strings).
