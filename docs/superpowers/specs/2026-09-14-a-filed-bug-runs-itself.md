# A filed bug runs itself

*wo-f35e603e, 2026-09-14. Issue #240. The close-trigger decision is Neo's, on question
289. Depends on the landing distinction drawn in
[2026-09-13-a-finished-order-proves-its-code-landed.md](2026-09-13-a-finished-order-proves-its-code-landed.md)
— §3 below is that spec's rule applied to the tracker, and reads oddly without it.*

## 1. The problem

`bugreport.report_bug` was `create_issue` + `_notify` and nothing else. Grepping all of
`src/jarvis/` for `gh issue close`, `close_issue` or `--add-label` returned nothing: the
OS had no issue lifecycle at all. An issue was created and abandoned.

So a tracker the OS writes to was a tracker only the human maintained. On 2026-09-14
issues #232, #233, #227 and #224 were all closed BY HAND after their work orders
completed, and #239 was filed with nothing scheduled to act on it.

The workflow asked for, in the user's words:

1. it creates the git issue *(this already worked)*
2. it creates a work order for the issue, and the issue moves to "in progress"
3. do the work *(the ordinary way — nothing new)*
4. once the work order is complete, the git issue is automatically closed

## 2. Not every bug becomes work

Every agent in the fleet carries the `report-jarvis-bug` skill, so an unconditional
"yes" at step 2 would let any worker commit the fleet to unbounded new work and
unbounded spend, with nobody deciding.

`catalog.BugsConfig` is therefore per project with a fleet fallback at `os.bugs`, on
`_parse_inspect`'s field-level inheritance — the shape already used for
`inspect.alarm_*`, copied rather than reinvented. `auto_work_order` **ships off**.

Two things about *which* project the setting is read from, because it is not the obvious
one. The project that NOTICED the bug has no say: `jarvis bug report` is run from
wherever the symptom appeared, and that project pays for nothing. The project consulted
is the one that would DO the work — the one whose git `origin` IS the tracker repository
(`issues.tracker_project`). Consent belongs to whoever pays.

Dispatch does **not** pass through Neo. A per-filing model call cannot bound total spend
the way an off switch does, and it would put a Claude call on the path of every bug
report, including the ones filed while the OS is stopped.

`bugs.auto_work_order` is in `catalog.SAFETY_KEYS`. By the letter of that list's rule it
is money rather than safety; by its spirit it is exactly safety, because switching it on
changes what a *worker* is allowed to do.

## 3. What closes the issue: the landing, not the completion

`completed` does not prove the code reached `main` — that is issue #232, and
`landing.assess` / `invariants.check_work_lands` shipped in 0.9.7 because of it. Closing
an issue on `completed` alone re-introduces that gap on the tracker, where it is more
visible and less recoverable: a bug marked fixed in public.

So the closing signal is the merge (`ops.complete_merged`), with one carve-out Neo ruled
on in question 289. A work order that produced **no code to land at all** — a not-a-bug,
a docs-only fix, one already fixed elsewhere — has no pull request to wait for, and
leaving its issue open for ever is the hand-maintained tracker this whole change exists
to end. `ops.mark_done` already embodies that asymmetry: it RECORDS unlanded work rather
than refusing it (kn-b437a0e5). The same `ProjectStore.work_unlanded_open` that keeps a
stranded order out of `CLOSED` therefore lets a produced-nothing order in.

## 4. Desired state, not a schedule of pokes

The OS never fires "label it now" at GitHub and hopes. Three pieces:

* `issues.desired_state(store, wo)` derives where the issue belongs — `IN_PROGRESS`,
  `RELEASED` (open, unlabelled) or `CLOSED` — from the work-order record alone.
* `issues.apply` moves the issue there, re-reading it first.
* `work_orders.issue_state` records what was last applied. It is `pr_url`/`pr_state`'s
  shape with the arrow reversed: `pr_state` caches what GitHub told us, `issue_state`
  caches what we told GitHub.

`Daemon.sync_issues` is then a comparison. Four properties fall out of that rather than
out of a guard per call site:

**Reversal (issue #240 C).** A failed or cancelled order maps to `RELEASED`; so does one
whose pull request was closed unmerged, via `pr_closure_told` — which REVERSES ITSELF
when the pull request is reopened, because nothing was written down once.

**Idempotency and reopening (D).** A second `apply` is a no-op. `apply` never fights a
human: an issue a person closed is left closed whatever the work order is doing, and one
they reopened after the OS closed it is not touched again, because the work order is
settled and its desired state has not changed.

**No second work order for one issue.** `ProjectStore.work_orders_for_issue` hands back a
LIVE work order instead of filing another; an issue whose work orders have all settled is
free to get a new one, which is what a reopened issue needs.

**`gh` unreachable (E).** Every write raises, `issue_state` is written only after GitHub
accepted the change, so a tick that could not reach `gh` leaves the two disagreeing and
the next sweep retries. The user hears once per daemon run
(`Daemon._warn_issue_sync_broken`, `_warn_pr_poll_broken`'s twin).

**Cost.** One indexed query per project per poll cycle, returning nothing for any project
that has never filed a bug. No subprocess at all while the tracker already says the right
thing.

### The order of the two writes when closing is load-bearing

The label comes off BEFORE the close. A close that fails is then retried against an issue
that is still open, and gets its comment exactly once. Closing first and failing on the
label would leave the next sweep facing an already-closed issue with `issue_state`
unwritten — the one shape that posts a duplicate comment.

## 5. Every GitHub write is in one file

`github.py` may not build a write verb, and `tests/test_github_artifact.py` walks its AST
to prove it. That property is load-bearing for the validation panel's blind review (Neo,
question 251): a seat that could comment on the pull request could talk to the implementor
it is judging.

So the lifecycle lives in a new `src/jarvis/issues.py`, which declares `ISSUE_VERBS` and
gets the mirror image of that test. "What can Jarvis write to GitHub" has exactly one
answer, in one list, and cannot grow without a commit that also edits a test.

`issues.checked_issue_url` is stricter than `github.checked_pr_url`: that one skips its
repository check when `origin` cannot be read, because losing PR polling over a moved
checkout would be worse than the exposure. Here the repository is a constant
(`bugreport.bug_repo()`), so there is no degraded case to be lenient about and a URL
anywhere else is refused outright.

## 6. What the user is told

`report_bug` raises only if the ISSUE could not be created — its existing rule, that a
ping must never be sent about a state that was not reached. Nothing after that raises:
by then the issue exists, and failing the call would report "not filed" about a bug that
was filed.

What did and did not happen instead comes back as `pickup` and is rendered by
`bugreport.pickup_note` into one line on the notification and on the CLI: the work order
id, or the reason there is none, or — when the work order exists but the label did not
reach GitHub — that the tracker does not show it yet and the OS will retry.

## 7. Deliberately not built

* **No retrofit onto existing issues.** The lifecycle belongs to bugs filed THROUGH the
  OS; an issue that predates the work order it is linked to carries no `issue_url` and
  is invisible to the sweep. Closing issues a human opened, off a heuristic match, is a
  much larger claim than this one.
* **No reopening, ever.** The OS closes and labels; only a person reopens. An OS that
  could reopen an issue could argue with the user about one.
* **No `--no-work-order` flag on `jarvis bug report`.** The project's config already
  decides, and a per-call override would let the caller — an agent — opt itself out of
  the policy that exists to bound it.
* **No assignee, no milestone, no project board.** One label is the whole of the
  in-progress signal, and every additional field is a second thing that can go stale.
