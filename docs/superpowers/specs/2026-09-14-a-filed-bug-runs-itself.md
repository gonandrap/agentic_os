# A filed bug runs itself

*wo-f35e603e, 2026-09-14. Issue #240. The close-trigger decision is Neo's, on question
289; the routing decision is the user's, given in two messages on the work order and
recorded in §2–§4. Depends on the landing distinction drawn in
[2026-09-13-a-finished-order-proves-its-code-landed.md](2026-09-13-a-finished-order-proves-its-code-landed.md)
— §5 below is that spec's rule applied to the tracker, and reads oddly without it.*

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

## 2. Priority routes the bug, and nothing else does

Every bug filed through `jarvis bug report` carries one of five levels, and the level is
the whole of the routing policy:

| level | what happens |
|---|---|
| `blocker`, `critical` | re-assessed by Neo (§3); if confirmed, a work order is created and a release ships once that work LANDS (§6) |
| `high`, `medium`, `low` | queued in the backlog. No work order, no release. The user promotes it when they choose |

**`--priority` is a required input.** No default and no inference: a filing without one
is an error and the command says so, with the rubric. That is the user's ruling and it
is the safe shape — a default is a value nobody chose, and the two levels that matter
are the two a default would have to be careful not to reach by accident. Requiring the
flag removes the question rather than answering it. The level is also visible on the
issue as a `priority: <level>` label, so the tracker shows it without anyone opening
the OS.

**There is no per-project switch.** An earlier draft made pickup a `bugs.auto_work_order`
config flag; the user's ruling replaced it — routing is by priority, *and only by
priority*. Two mechanisms deciding the same question is one mechanism too many, and the
config version had the wrong shape anyway: it made the answer depend on which project
was asked rather than on how bad the bug is. `catalog.BugsConfig` survives with one
field, `label`, which is only the NAME of the in-progress label.

**Which project does the work** is still not the one that noticed. `jarvis bug report`
runs wherever the symptom appeared, and that project pays for nothing. The project used
is the one whose git `origin` IS the tracker repository (`issues.tracker_project`).

## 3. A `critical`/`blocker` rating is a claim, not a verdict

Every agent in the fleet carries the `report-jarvis-bug` skill, and an agent that has
just lost an hour to something rates it `critical`. So the two levels that commit the
fleet to a fix and a release are RE-ASSESSED BY NEO before anything acts on them
(`issues.ask_triage`, a new `triage` question kind). Neo confirms → the work order is
created. Neo downgrades → the backlog, like any other bug. `low`, `medium` and `high`
are not re-assessed: they commit the fleet to nothing, so there is nothing to guard.

**The rubric** (`issues.PRIORITY_RUBRIC`) is one text, shown in three places: the
`jarvis bug report` interface, the error when the flag is missing, and the prompt Neo
judges against. One wording, because a rubric that exists in two wordings is two
rubrics, and the point is that the levels mean the same thing to every agent.

The top two are defined **by effect on the fleet**, never by inconvenience — the user's
instruction, and also the only definition that survives self-rating:

* `blocker` — the fleet cannot work. Nobody makes progress until it is fixed.
* `critical` — work is LOST, or the OS LIES: state destroyed or silently dropped, or a
  surface reporting something untrue so the user acts on a false picture. *A work order
  dying is not this; a work order dying WITHOUT SAYING SO is.*
* `high` — a real defect with a workaround.
* `medium` — annoying or wasteful but bounded to one surface or command.
* `low` — cosmetic, a rough edge, a suggestion.

**Fail closed.** The backlog is where every bug lands, whatever its level, and a
dispatching claim is promoted OUT of it only by a confirmation. So Neo being off,
unreachable, escalating, or answering unintelligibly leaves the bug exactly where it is
— queued, visible, and reported as *unconfirmed*. There is no path from a filing to a
work order that does not go through a verdict. An unconfirmed `blocker` that quietly
became a release is much the worse failure.

A downgrade that names no level lands on `issues.SAFE_DOWNGRADE` (`high`), and a
downgrade that names a HIGHER level is read as the refusal it is
(`issues.downgrade_to` fails towards `high` in both directions): `deny` plus `blocker`
is a contradiction, and the safe reading of a contradiction is the one that spends
nothing.

**The claim and the verdict are both kept.** The moving `priority:` label carries the
level the bug ACTUALLY has after Neo — one label, never two. The ORIGINAL claim is
written into the issue body at filing time (`Priority claimed by the reporter`), where
no re-assessment can move it, and `issues.triage_comment` posts both levels on the issue.
That disagreement is the signal that says whether the rubric is working, so nothing may
overwrite it silently.

**Neo's reasoning is not published** (review round 2). It is model prose written for the
internal record by a model that does not know it will be published, and Neo answers with
fleet context behind it — learnings, other work orders, project names, paths. It rides
the private half of the record instead: the Neo question row holds it verbatim
(`jarvis neo show <id>`), and the inbox row `Daemon._deliver_triage_verdict` writes
carries its head plus a pointer to the rest. Same rule as `closing_comment` (§9), for the
same reason: a GitHub comment is indexed and cached whether or not it is later deleted,
and nobody reads this one before it leaves the machine.

The Neo question row IS the pending-triage record (`context` carries the issue URL, the
title, the claim and the backlog id). There is no second table and no orphan state: a
Neo that never answers leaves a backlog item and a question, both of which a person can
already see.

## 4. What closes the issue: the landing, not the completion

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

## 5. Desired state, not a schedule of pokes

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

## 6. The ship goes through the ordinary release machinery

A confirmed `critical`/`blocker` whose fix LANDS earns a release. What the OS files is
not a release: it is a WORK ORDER (`Daemon.ensure_release`) whose brief says to run

    scripts/shipit.sh --stage --wo <its own id>

and nothing else. Three things about that, each of them the user's instruction:

* **It is the existing path, not a second one.** A release is a privileged action, so
  that work order hits the gate exactly as a human-filed release would. A bug filing
  must not bypass a gate a human filing would face, and the brief tells the worker the
  gate is expected rather than a failure.
* **`--stage`, never an inline restart.** Restarting inline kills the worker's own
  session mid-turn: 0.5.1 landed as `failed` after a perfect deploy, which is why
  staged mode exists at all. The brief forbids dropping the flag.
* **Landed, not completed.** The trigger is the tracker reaching `CLOSED` *with a pull
  request behind it*, which is the merge signal of §4 — a fix sitting on an unmerged
  branch never gets here, and neither does the produced-nothing carve-out, because a
  release carrying nothing is a restart of the whole fleet for an empty tag.

**Batching.** Two blockers landing ten minutes apart are ONE release. An open release
work order takes the second fix into its `metadata[release_for_issues]` list and appends
it to its description, because a release ships whatever is on `main` and the first one
has not gone out yet. Only once that order has settled does the next landed fix earn a
new one — the rule is "one release in flight", not "one release".

## 6a. The wiring a new event kind and a new question kind each owe

Two mechanisms in this OS have a list of places that must be told about a new member,
and both lists are longer than they look (review round 4).

**Four new event kinds** — `issue_in_progress`, `issue_released`, `issue_closed`,
`release_batched` — are named in `timeline._describe`. An unregistered kind is not
hidden, it renders as the bare kind string beside a raw JSON payload, and that is not
the same claim as a reader being able to see it (kn-3f133363). The question to ask is
which surface is a kind's ONLY one: for these four the timeline is it, because the issue
shows the result but never when the OS decided it, and `issue_state` is a column nothing
renders.

**`triage` is a Neo question kind with an EMPTY `wo_id`**, the first of its sort, and a
question kind is a claim about who is waiting (kn-4edb0eb7). For `triage` the answer is
nobody: the bug is sitting in the backlog and no work order exists. All seven sites are
told, and all seven say the same thing — the resolution is `jarvis backlog promote`:

* `neo_store.Q_KINDS`, and `Daemon._neo_drain`'s `deliver()` branch, which returns FIRST
  because every branch below it reaches for `q["wo_id"]`.
* `ops._neo_attention` / `os_status` — the question is still LISTED, because an
  unconfirmed `blocker` nobody hears about is the failure this whole path exists to
  prevent, but its `decide` line names the backlog item rather than `jarvis neo answer`.
* `invariants.check_neo_escalations_are_live`, with `_stale_triage_question`. Its subject
  is a CENTRAL backlog item rather than a row in the project store, so ownership is
  resolved through `registered_project_paths` — otherwise every project would report the
  same question on the same tick.
* `ops.neo_answer_escalated`, refused: the delivery would look up `wo_id=""`.
* `ops.neo_review`'s `--correct` tail, which forwards to the worker whenever the order is
  not terminal. Stated rather than left to the lookup failing, because this is the
  command the OS itself tells the user to run. The learning is still recorded — that is
  what teaches the rubric.
* `ui/templates/_question.html`'s `answer_form`, which otherwise offers a textarea
  labelled "it goes straight to the worker". The template and the `ops` guard have to
  move together: either alone leaves one of the two surfaces open.

## 7. What the user is told

`report_bug` raises only if the ISSUE could not be created — its existing rule, that a
ping must never be sent about a state that was not reached. It also raises, BEFORE
anything is filed, when `--priority` is missing or unknown; the error carries the rubric.
Nothing after the issue exists raises: failing the call would report "not filed" about a
bug that was filed.

What did and did not happen instead comes back as `pickup` and is rendered by
`bugreport.pickup_note` into one line on the notification and on the CLI. It never
rounds a claim up to a decision: between the filing and Neo's verdict the honest answer
is that nothing has been decided yet, and that is what it says — along with the backlog
id, or the work order id once there is one, or the note that the priority label did not
reach the tracker and the OS will retry.

## 8. Deliberately not built

* **No retrofit onto existing issues.** The lifecycle belongs to bugs filed THROUGH the
  OS; an issue that predates the work order it is linked to carries no `issue_url` and
  is invisible to the sweep. Closing issues a human opened, off a heuristic match, is a
  much larger claim than this one.
* **No reopening, ever.** The OS closes and labels; only a person reopens. An OS that
  could reopen an issue could argue with the user about one.
* **No per-project opt-out and no `--no-work-order` flag.** Priority is the routing
  policy; a second switch would let a caller — an agent — opt itself out of the
  re-assessment that exists to bound it.
* **Neo does not re-assess `low`/`medium`/`high`.** They spend nothing, so a model call
  on each would be pure cost for a decision nobody acts on until the user promotes it.
* **No assignee, no milestone, no project board.** One label is the whole of the
  in-progress signal, and every additional field is a second thing that can go stale.
* **NO MODEL PROSE IS EVER PUBLISHED.** One rule, two comments. The closing comment
  carries the work order id, the title and the pull request link; the triage comment
  carries the two levels and where the bug went. Neither carries `result_summary` or
  Neo's reasoning. Both are written by a model that does not know it will be published,
  to a tracker that is public and that indexes and caches a comment whether or not it is
  later deleted, with nobody reading it in between. Anything a person needs beyond those
  fields is on the work-order record, one command away.
* **The test harness does not point at the real tracker.** `testing.fake_gh` makes
  `bug_repo()` answer `jarvis-fixture/no-such-tracker`, so `checked_issue_url` still has
  a repository to enforce, and a test that got past the fake `gh` would write to a
  repository nobody owns rather than to live public issues. `BLOCKED_GH` is then the
  second line of defence, not the only one.
