# A planner submits behind its spec pull request

wo-3312682f · GitHub issue #822 · project `jarvis_os`
Ruling: Neo question 877 (answering for the user). Knowledge: kn-ae871d91, kn-03b735b4,
kn-1f2f870b, kn-b437a0e5 — cited, not restated.

## The problem

**Every feature-order planner that gets as far as a valid plan is refused by
`ops.finish`, and the refusal names a flag that does not exist.** Four anchors close the
loop:

1. `ops.submit_plan` (`src/jarvis/ops.py:7118`) settles the planner at its tail with
   `finish(fo["plan_wo_id"], f"submitted a plan for {fo_id}: …")` — no `pr_url`, no
   `abandon`.
2. `ops.finish` (`src/jarvis/ops.py:4851`) runs `unlanded_work(store, _wo, pr_url or "")`
   and, on `stranding.produced and not abandon`, raises
   `OpsError(unlanded_refusal(...))` (`:4852`).
3. The planner ALWAYS produced something. `submit_plan` requires
   `landing.committed_text(path, planner_wo, plan["design_doc"])` to resolve
   (`ops.py:7160`) and `plans.spec_problems` to pass over that committed text, so a
   submittable plan implies at least one commit on the planner's branch. There is no
   quiet path for a planner with a real spec.
4. `ops.unlanded_work` (`ops.py:3789`) reads the pull request from
   `store.get_work_order(wo["id"])["pr_url"]` and from its `pr_url` argument — **never
   from the branch**. Nothing on the `jarvis fo plan` route can write either:
   `cli.py:777-784` exposes `fo_id`, `--from-file`, `--project` and nothing else.

Consequence, and it is the shape of the defect rather than an inconvenience: the planner
opens a spec pull request, submits, is refused, and the only remedies
`unlanded_refusal` prints (`ops.py:4747`) are `--pr <url>` and `--abandon "<why>"`.
`--pr` is not a `jarvis fo plan` flag, so the printed fix cannot be followed; `--abandon`
is a LIE about a pull request that exists. kn-1f2f870b and kn-b437a0e5 are the two
recorded planners that hit this.

Root cause, stated plainly: `work_orders.pr_url` has exactly two writers
(`ops.finish --pr` and `gates._record_pull_request`, per `ops.declared_pull_request`'s
docstring at `ops.py:3740`) and the plan-submission route reaches neither, while
`unlanded_work` refuses to look at the branch. This spec fixes the missing writer. It
does NOT change `unlanded_work` to read the branch — that would move a network call into
every settling and is out of scope.

## The fix

Ruling (B): `ops.submit_plan` resolves the branch's OPEN pull request over `gh` and
records it **non-declaratively**, the way `gates._record_pull_request` does
(`src/jarvis/gates.py:1474`), so the planner settles `completed` at once.

### 1. The read — `src/jarvis/github.py`

New function beside `pr_view`:

```python
def open_pull_request_for_branch(branch: str, cwd: Path | None = None) -> str:
```

* Runs `gh pr list --head <branch> --state open --json url` through `github._run`
  (`github.py:370`), so a missing binary raises `GhUnavailable`/`GitHubError.NO_GH` and a
  `gh` that ran and refused raises `GitHubError` with `REFUSED`/`TIMEOUT` — the
  distinction `GhUnavailable`'s docstring exists for (`github.py:122`).
* Returns `""` when the array is empty. Unparseable JSON raises
  `GitHubError(..., UNREADABLE)`, exactly as `pr_view` does at `github.py:410`.
* Passes the first `url` through `github.checked_pr_url(url, cwd)` (`github.py:138`)
  **before any caller may hold it**, so the URL is shape-checked and origin-checked like
  every other recorded `pr_url`.
* `("pr", "list")` is added to `READ_ONLY_VERBS` (`github.py:66`). It is a read;
  `tests/test_github_artifact.py`'s AST walk will otherwise fail, and that failure is the
  mechanism working.
* **Branch safety.** `branch` becomes an argument, so it is validated against
  `github.BRANCH_RE` (`github.py:438`) first and refused with
  `GitHubError(..., URL_REFUSED)` otherwise — the same guard
  `branch_protection` applies at `github.py:471`. That bounds it to
  `^[A-Za-z0-9][A-Za-z0-9._/-]*$`: no leading `-`, so `gh` cannot read it as a flag, and
  no whitespace or shell metacharacters. The value's provenance is narrow anyway —
  `landing.authored(worktree).branch`, read out of the planner's own git worktree by the
  OS — but the guard is what makes it safe to say so.
* `_run`'s `url=` parameter is only used to render the command in errors; pass the branch
  so a failure reads ``gh pr list nice-branch` failed: …``.

### 2. `ops.submit_plan` records it — and why that keeps the `completed` route

```python
store.update_work_order(planner_wo["id"], pr_url=pr_url)
store.add_event(planner_wo["id"], "pr_url_recorded",
                {"pr_url": pr_url, "feature_order": fo_id, "source": "plan_submit"})
```

`source` is **`"plan_submit"`**, not `"gate"`: the two writers must stay
distinguishable on the record — a gate approval and a plan submission are different
events and a reader (`jarvis wo show`, the dashboard, the landing sweep) should not be
told a gate happened.

Why this leaves the planner on the `completed` route, precisely:
`ops.routes_on_pull_request` (`ops.py:3769`) is a NEGATIVE test — the column routes
unless a `pr_url_recorded` event exists with no declaration behind it, and
`ops.declared_pull_request` (`ops.py:3740`) counts only a `pr_url` inside a `finished`
event payload. A `pr_url_recorded` event with `source: "plan_submit"` and no
`finished {pr_url}` therefore makes `routes_on_pull_request` false, so `land_finished`,
the reconciler's park and `Daemon.poll_pull_requests` all leave the planner `completed`.
`source` is NOT read by any of them; it is for humans.

If it were written as a declaration instead (the rejected fix A, below): the planner
would park in `waiting_pr_merge`, a validation panel round would open over EVERY spec
pull request, and the merge poller would watch a pull request the OS never asked anyone
to merge.

### 3. Where in `submit_plan` — after the spec check, before any write

Order inside `submit_plan`: status check → `plans.parse_plan` →
`landing.committed_text` + `plans.spec_problems` → **the lookup and its refusals** →
`_ask_plan_review` → `store.update_feature_order` / `set_feature_status`.

Two reasons, both load-bearing:

* `submit_plan`'s own docstring rule (`ops.py:7119`): "The plan is validated first, so a
  bad plan costs a revision and nothing else — no work order, no Neo call, no state to
  unwind." A missing pull request is a submission defect and must cost the same nothing.
* Refusing at the trailing `finish()` — today's behaviour — produces the half-state
  kn-03b735b4 records: plan stored on the feature order, `plan_review` set, Neo's review
  question already asked, planner still `running` and the `OpsError` surfacing to the
  planner as if nothing had been accepted. The refusal must move ahead of the first
  write so the record is untouched.

It goes AFTER the committed-copy check because that check is what proves a commit exists
and names the branch (`_spec_branch`, `ops.py:7079`), and because a planner who has not
committed its spec should be told to commit, not told to open a pull request.

### 4. It runs only when there is something to land

Guard, before any `gh` call:

```python
if planner_wo and not store.get_work_order(planner_wo["id"]).get("pr_url"):
    work = authorship(store, planner_wo)     # ops.py:3827
    if work.produced:                        # landing.Authored.produced
        ...look up, record, or refuse...
```

* `ops.authorship` is the read without the pull-request short-circuit — the same call
  `finish` already pays for, so this adds one worktree read, not a network round trip, on
  the quiet path.
* A planner whose worktree produced nothing (`produced` false, including `unreadable`)
  **makes no `gh` call and behaves exactly as today**: `unlanded_work` will not refuse it
  either. That is the 60-planner exclusion `unlanded_work`'s docstring protects; do not
  regress it.
* A record that already carries a `pr_url` makes no `gh` call — nothing to learn, and a
  submitter's declaration is never overwritten (same fourth condition as
  `gates._record_pull_request`).

### 5. The three refusals, with their content

All three are `OpsError`, raised at the position in §3.

**(a) No open pull request on the branch.** `open_pull_request_for_branch` returned `""`.

> `{fo_id}`'s planner has committed `{design_doc}` on `{branch}`, and there is no OPEN
> pull request on that branch — so the spec would stay on the branch and nothing would
> ever land it. Push the branch and open a pull request, then run `jarvis fo plan` again:
> the plan is not stored until this passes.
> `git push -u origin {branch} && gh pr create --fill`

**It must not contain the string `--pr`**, and a test asserts that: `jarvis fo plan` has
no such flag (`cli.py:777`) and the unfollowable remedy is half the reported bug.

**(b) `gh` could not be asked.** Catch `github.GitHubError` and say WHICH, because a PATH
problem and a credentials problem have opposite remedies (`GhUnavailable`'s docstring,
`github.py:122`):

* `GhUnavailable` → "the `gh` CLI is not installed where the OS can reach it, so the
  planner's pull request cannot be confirmed" plus the `GH_BIN` override / PATH note.
* any other `GitHubError` → quote `e.reason` (the module's own fixed vocabulary, never
  `gh`'s stderr — `GitHubError`'s docstring, `github.py:76`) and say the plan was not
  stored and can be resubmitted.

Both end with: resubmit once `gh` works. Neither suggests `--abandon`: the work is not
being abandoned.

**(c) Already recorded.** No message, no `gh` call, no new event: submission proceeds as
today. A planner whose merge gate was decided before submission is exactly this case, and
`ops.declared_pull_request`'s docstring already describes it settling `completed`.

## Rejected alternatives

* **(A) Thread a `--pr` flag through `jarvis fo plan` into the trailing `finish()`.**
  Rejected by ruling 877. A `finish --pr` writes `finished {pr_url}`, which IS a
  declaration, so `routes_on_pull_request` becomes true: every planner parks in
  `waiting_pr_merge` and a validation panel round opens over every spec pull request by
  default. That breaks the house rule that new panel behaviour ships disabled, and it
  makes the user merge-gate documents.
* **Teach `ops.unlanded_work` to look for the branch's pull request.** Fixes every route
  at once and is therefore worse: it puts a 30s-timeout network call
  (`github.GH_TIMEOUT`) inside the predicate three settling paths and the reconciler call,
  and its docstring's "THE PULL REQUEST IS READ FROM THE RECORD, NEVER FROM `wo`"
  invariant exists to keep it cheap and deterministic.
* **`--abandon` from `submit_plan`.** Records "this work is deliberately not being landed"
  about a spec the children are built from. False on the record, and kn-ae871d91 is what
  happens when the record lies about landing.
* **Let planners pass the whole check.** Exempting a work-order kind is the "listed kinds
  that would rot" failure `unlanded_work`'s docstring rejects, and a planner's spec really
  does need to land.

## Tests

New file `tests/test_planner_pull_request.py`, sitting beside
`tests/test_pr_recorded.py` (INV-PR-RECORDED: every settling route records, decides, or
refuses) and `tests/test_work_lands.py` (the two landings that already refuse). Reuse
`test_pr_recorded.py`'s `_git` helper and `started` fixture shape — a repository with a
real default branch and an `origin`, without which `landing.authored` answers
`unreadable` and every assertion passes vacuously. Reuse `a_plan` / `child` / `ASK` from
`tests/test_feature_orders.py` (the home of `submit_plan` behaviour) and the
`planning(fleet)` fixture pattern from `tests/test_plan_side_effect.py:30`. Fake `gh` by
monkeypatching `jarvis.github.open_pull_request_for_branch`, plus one test that drives
the real function with a stub binary via the `GH_BIN` env override
(`bugreport.gh_bin`, `bugreport.py:197`).

1. `test_a_planner_with_an_open_pull_request_submits_and_completes` — committed spec +
   `gh` returns one open URL. Asserts: `submit_plan` returns `status: "plan_review"`,
   the planner's `status == "completed"`, `pr_url` equals the URL on the record, and one
   `pr_url_recorded` event with `source == "plan_submit"`.
2. `test_a_recorded_planner_pull_request_does_not_route` — same setup, then a full
   `fleet.drain()`. Asserts the planner never reaches `waiting_pr_merge`, no validation
   round is opened over it, and `ops.routes_on_pull_request` is false for it. This is the
   guard on ruling 877's reason for rejecting fix (A).
3. `test_no_open_pull_request_refuses_and_stores_nothing` — `gh` returns an empty array.
   Asserts `OpsError`; the message names the branch, contains neither `--pr` nor
   `--abandon`; and the feature order's `status`, `plan` and `plan_question_id` are
   UNCHANGED, no `plan_submitted` event exists, and the planner is still `running`
   (kn-03b735b4's half-state).
4. `test_gh_unavailable_refuses_saying_so` and
   `test_gh_refusal_refuses_with_its_reason` — the two `github` failures. Assert the
   messages differ, that the `GhUnavailable` one talks about installation/PATH and the
   other does not, that no remote stderr text appears, and that nothing was written
   either time.
5. `test_a_planner_that_already_has_a_pr_url_makes_no_gh_call` — the record carries a
   `pr_url` up front; the fake raises `AssertionError` if called. Asserts it submits and
   completes. Pair it with
   `test_a_planner_that_authored_nothing_makes_no_gh_call` (same fake, a planner with no
   commits and no design-doc requirement path) to prove point §4's quiet path.
6. `test_open_pull_request_for_branch_refuses_a_branch_it_may_not_ask_about` in
   `tests/test_github_artifact.py`'s neighbourhood — a branch starting with `-` raises
   `GitHubError` with `URL_REFUSED` and runs no subprocess; a URL on another repository
   raises `UntrustedPullRequest` through `checked_pr_url`.

## The second half: the planner settle is not the submission

Ruling: Neo question 903 (answering for the user), on the reporter's correction to their
own filing of #822.

**The class, not the instance.** Everything above removes ONE way the trailing
`finish(fo["plan_wo_id"], …)` can refuse over a submission that has already written
everything. It is not the only one: `ops.finish` also raises
`OpsError(gate_still_open(...))` on `store.open_approvals(wo_id)` (`ops.py:4836`), and
planners do file merge gates (kn-ae871d91). Both instances share one cause — the plan
submission and the planner settle share an exit code. The reporter read `error:` over a
command whose every write succeeded, concluded nothing had been submitted, and retried
three times; each retry re-stored the plan and asked a FRESH Neo review question for the
same feature order.

`submit_plan` therefore catches `OpsError` from that call and returns normally:

* `out["warning"]` carries the failure — the key `ask_question` already uses
  (`ops.py:7720`), rendered as a `warning:` line — naming the open approval ids and the
  exact `jarvis wo finish <planner> --summary "…"` that settles the planner once the
  blocker is cleared, with `finish`'s own refusal quoted underneath.
* `out["planner"]` is ABSENT in that case. A reader must not be told the planner settled
  when it did not.

**Why `OpsError` is the boundary.** An `OpsError` is a refusal this module wrote: it is
a state of the record someone chose to describe, and describing it in a warning loses
nothing. Any other exception is a defect nobody has read — swallowing it under a
`warning:` line would turn a bug in `finish` into text a planner is asked to act on. So
the catch is `except OpsError` and never `except Exception`, and
`test_a_non_ops_failure_from_the_planner_settle_still_propagates` holds that line.

`test_an_open_gate_warns_and_the_submission_stands` asserts all four: no raise, the plan
stored exactly once, exactly one Neo review question, and a warning naming the open
approval id.

## Out of scope

`unlanded_work` reading branches; `jarvis fo plan` gaining any flag; whether spec pull
requests should be merged or validated at all (ruling 877 says not by default);
the stale-`pr_url` case `test_pr_recorded.py` documents as `wo-cd73c537`; closed and
merged pull requests on the branch — `--state open` only, and a planner whose spec PR
already merged still has no open one and gets refusal (a), which names that case: the
branch is RESET onto `origin/main`, never given a second pull request. A squash merge
leaves the branch's own commits absent from main, so `landing.authored` still reports them
and the guard still fires — kn-ae871d91's incident, and the sentence is what stops the
next planner repeating it.
