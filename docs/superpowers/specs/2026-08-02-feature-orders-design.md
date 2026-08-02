# Feature orders — a planned unit of work above the work order

*2026-08-02*

Analysis and design recommendations. No implementation is proposed for this document's
own change set; the phasing section at the end is what a first pull request would cover.

## Problem

A work order is the only unit of work the OS has, and it is welded to a single Claude
session: `jarvis wo create` inserts a `pending` row, the daemon claims it, `dispatch.py`
composes one briefing, `worker_session.start()` opens one conversation, and the work order
ends when that conversation ends. One row, one session, one pull request.

That equivalence is the reason the OS works — the whole state machine, the gate, the
review queue and the attention list are all defined over "a conversation Jarvis owns" —
and it is also the ceiling. Everything the user wants done has to arrive already
decomposed into single-session pieces, so the decomposition happens in the user's head,
in chat, before any of it reaches the OS. The OS never sees the feature; it sees the
fragments the user managed to describe, in the order the user thought of them, with the
dependencies between them held nowhere but the user's memory of what they typed.

Three concrete consequences, all visible in the live fleet today:

**Decomposition is unrecorded work.** Splitting "add project-level budgets" into six
sessions is real design work — sequencing, interface choices, what is testable when — and
today it produces no artefact. The six `jarvis wo create` calls are its only trace.

**Ordering is manual and lossy.** Work order B needs A's schema change. Nothing in the OS
knows that. The user either dispatches A, waits, watches, and then dispatches B by hand,
or dispatches both and gets two workers editing the same file in two worktrees.

**The user is the scheduler.** Prime directive 7 is "keep context lean" and the OS's whole
purpose is to keep the user's attention budget small, but the user is currently the only
component that holds a multi-session plan.

The ask is a coarser entity — a **feature order** — that the OS can be given directly, and
that a project's orchestrator *plans* into a dependency-ordered set of ordinary work
orders before any of them runs.

## What already exists

Most of the parts are in the tree already, scattered across two stores. Naming them
first keeps this design from rebuilding them.

| Piece | Where | State |
|---|---|---|
| Dependency edges | `central_store.py` — `backlog.depends_on` (JSON list of ids), `unfinished_dependencies()`, `promote_backlog(..., force=)` | Exists, on backlog items only |
| Promotion of a plan item into a work order | `ops.promote_backlog()` | Exists, manual — a human runs it |
| The OS filing its own work order | Neo verdicts carry an optional dispatch → `daemon._dispatch_neo_cleanup()` → `origin='neo'` with a `pre_approved` marker in `work_orders.metadata` | Exists, shipped and tested |
| Per-work-order agent configuration | `work_orders.model / effort / permission_mode / append_system_prompt`, resolved in `worker_session.briefing_for()` | Exists — a differently-briefed worker needs no new column |
| Shipping OS-authored agent assets into a worker | `bootstrap.install_agent_skills()` materializes `assets/skills/` into the project's gitignored `.jarvis/agent-skills/.claude/skills/`, handed over on every turn as `--add-dir` | Exists, for skills |
| Derived, per-work-order "what does this need from me" | `invariants.true_blockers()` | Exists, per work order only |
| Review of an agent's judgement before it is acted on | assumptions + `jarvis wo review`, and Neo as first responder | Exists |

So the missing pieces are narrower than they look: **a parent that owns a set of work
orders, dependency edges between work orders rather than backlog items, a scheduler that
respects them, and a planning step that produces them.**

## Verified behaviour (Claude Code 2.1.220, tested live 2026-08-02)

The multi-profile "team" part of the ask depends on two CLI facts. Both were checked
against the real binary before this design was written, with a control.

| Fact | Evidence |
|---|---|
| A headless `-p` turn can spawn subagents with the Task tool | probe returned the subagent's exact sentinel reply, `subtype: success` |
| `--add-dir X` exposes `X/.claude/agents/*.md` as subagent types — the same mechanism that already delivers skills from `X/.claude/skills/` | with `--add-dir`, the probe agent answered; **control**: the identical prompt without `--add-dir` returned `UNAVAILABLE` |

This matters more than it looks. It means the "team of profiled agents" needs **no new
transport, no new supervision and no change to `worker_session.py`** — agent definitions
ride in beside the skills on the `--add-dir` that every turn already carries. The team is
a content change, not an architecture change.

## Recommended design

### 1. Where planning runs: the planner is a work order

**Recommendation: a feature order's planning phase is an ordinary work order with a
different briefing.**

When the daemon picks up a `pending` feature order it does not fan out. It creates exactly
one child work order — the *plan* work order — briefed as the planning lead: read the
codebase, consult your profiled subagents, and finish by submitting a plan.

Everything the OS already does for a worker then applies to the planner for free: the
headless turn transport, the worktree, `jarvis wo ask` to Neo on doubt, assumptions, the
privileged-action gate, stall detection, the timeline, cancellation. The planner is
observable and interruptible through surfaces that already exist. Its `jarvis wo finish`
is the plan submission.

The rejected alternative — a planning pipeline inside the daemon, orchestrating
`claude_cli.run_headless()` calls in Python the way `neo.drain_queue()` does — is
covered under *Alternatives* below. Short version: it is a second execution model in the
daemon, and planning is precisely the task that wants a worktree and a codebase.

Two deliberate differences from an ordinary worker:

- **The planner does not write product code.** Its worktree is for reading and for the
  plan document. This is expressible today through the existing per-work-order
  `permission_mode` / settings path, without new machinery.
- **Its terminal action is structured.** `jarvis fo plan <fo-id> --from-file plan.json`,
  not prose. A plan is a graph; it has to be validated (cycles, unknown ids, child count),
  and validation needs a schema.

  The `--from-file` shape is not cosmetic. A recorded learning on this project is that
  `gates.scannable()`'s quote-blanking fails on nested and mixed quoting, so any long
  prose argument containing repo paths can leak a literal into the gate classifier and
  trip a false positive. A plan is a long argument full of repo paths. It goes in a file.

### 2. Data model: a new per-project table, plus two columns

**Recommendation: `feature_orders` lives in the per-project database, next to
`work_orders`.**

```
CREATE TABLE feature_orders (
    id TEXT PRIMARY KEY,                    -- fo-xxxxxxxx
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    origin TEXT NOT NULL DEFAULT 'jarvis',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    plan_wo_id TEXT REFERENCES work_orders(id),   -- the planner
    plan TEXT,                              -- the submitted plan, as JSON
    max_parallel INTEGER,                   -- slot cap for this feature's children
    needs_attention INTEGER NOT NULL DEFAULT 0,
    attention_reason TEXT,
    backlog_id TEXT,
    metadata TEXT
);
```

and on `work_orders`, two nullable additions through the existing `ADDED_COLUMNS`
migration in `ProjectStore._migrate()`:

```
parent_id  TEXT      REFERENCES feature_orders(id)   -- NULL for a standalone work order
depends_on TEXT      NOT NULL DEFAULT '[]'           -- JSON list of work-order ids
```

Why per-project rather than central: a feature order is scoped to one project by
construction — the user's framing is "each project orchestrator" — and putting the parent
in the same database as its children means one transaction, real foreign keys, and one
query for a listing. The backlog's central home is right for the backlog (an OS-wide
intake list that is not yet anybody's work) and wrong for this.

Why `depends_on` as a JSON list rather than an edge table: it matches
`backlog.depends_on` exactly, so `unfinished_dependencies()` generalizes rather than
being written twice, and the diff stays small. An edge table is the correct upgrade if
edges ever need *types* ("blocks" versus "informs, but does not block") — that is the
trigger to revisit, and nothing today needs it.

Why the backlog is left alone: a backlog item should be promotable into a feature order as
well as into a work order (`jarvis backlog promote <id> --as feature`), which is a small
addition to `ops.promote_backlog()`. The backlog stays intake; the feature order is
committed work.

### 3. Scheduling: the dependency rule

This is the load-bearing change and the riskiest one.

**Recommendation: do not add a `blocked` status. Filter at claim time.**

`Daemon.dispatch_pending()` loops on `store.claim_next_pending()`, which takes the oldest
`pending` row. The change is a `WHERE` clause: a work order is claimable only when every
id in its `depends_on` is satisfied. Blocked work orders stay `pending` and are simply
passed over.

The reason not to introduce a status is that this codebase's statuses are load-bearing —
`OPEN_STATUSES`, `TERMINAL_STATUSES`, `true_blockers()` and `settle_work_order()` all
switch on them, so a new one means touching every surface — and "blocked" is fully
*derivable* from `depends_on` plus the dependencies' statuses. Storing a derivable fact
invites drift between the stored value and the truth. The codebase already draws this line
in the right place: `waiting_pr_merge` became a status because nothing derived it, whereas
attention is re-derived every tick by `true_blockers()` and deliberately not stored.

What blocked work orders *do* need is to stop looking stuck. `pending` currently means
"will start as soon as a slot frees", and a row that will sit there for two days
contradicts that. Listings and the dashboard should render the derived form:
`pending — blocked by wo-1a2b3c, wo-4d5e6f`.

**When is a dependency satisfied?** This is the hardest question in the design and it does
not have a clean answer.

The obvious rule is `completed`. But the repo's real flow does not end at `completed` — a
worker finishes behind a pull request (`jarvis wo finish --pr`), the work order parks in
`waiting_pr_merge`, and it only reaches `completed` when the **user** merges and runs
`jarvis wo done`. Under a strict rule, every dependency edge in a feature order becomes a
mandatory human intervention, which is the exact cost this feature exists to remove.

Three options:

1. **Strict** — satisfied on `completed` only. Correct: the child branches from a `main`
   that contains its dependency. Slow: N human merges serialize the feature.
2. **Loose** — satisfied on `waiting_pr_merge` or `completed`. Fast, and wrong by default:
   the child's worktree is cut from `main`, which does not yet contain the dependency's
   code, so the child either cannot build on it or silently duplicates it.
3. **Stacked** — satisfied on `waiting_pr_merge`, *and* the child's worktree is created
   from the dependency's branch rather than from `main`. This is what a human team does
   with stacked pull requests, and it is the only option that is both fast and correct.

**Recommendation: strict by default in v1, with option 3 as the known destination.**
Stacking requires `worker_session.start()` to pass a base branch through to `--worktree`,
which is unverified CLI behaviour and needs its own live check before anything is built on
it. Do not ship the loose rule as a default; if it is offered at all it should be a
per-edge opt-in the planner must justify.

Strict has a hard prerequisite, and this is the most actionable finding in this document:
**`bl-54287b3f` (auto-complete a work order when its pull request is merged) stops being a
convenience and becomes a blocker.** Without it, a six-work-order feature costs six manual
`jarvis wo done` calls just to advance the schedule, and the feature order is slower than
doing it by hand. It should ship first.

**Slot fairness.** `max_concurrent` is per project, so a feature order that fans out eight
children will occupy every slot and starve the ad-hoc work orders the user creates from
chat — the responsiveness the OS is judged on. Recommendation: a per-feature-order
`max_parallel`, defaulting to `min(2, project.max_concurrent - 1)`, so at least one slot
always remains for work the user asked for just now.

### 4. The planning team

**Recommendation: ship agent profiles the same way skills are shipped, and start with
three, not the five in the original sketch.**

Mechanically: `src/jarvis/assets/agents/*.md`, materialized by `install_agent_skills()`
into `.jarvis/agent-skills/.claude/agents/`, reaching the worker on the `--add-dir` that
`briefing_for()` already passes. Verified above. The function is already documented as
owning and rebuilding its whole generated tree, so adding a sibling directory is a small,
well-precedented change — likely a rename to something like `install_agent_assets()`.

The three profiles:

- **architect** — decomposition and sequencing. Which pieces are separable, what the
  interface between them is, what must land first.
- **test lead** — what "done" means for each child. Every child needs acceptance criteria
  in its own description, because the child worker will never see the plan.
- **scope** — what the user actually asked for and what is explicitly out. The
  counterweight to a planner that keeps finding adjacent work.

"Product manager" and "project manager" from the original sketch are deliberately absent.
The planner *is* the project manager — sequencing and slot budgeting are its own job, and
a separate PM agent holds no information the lead does not. A distinct product voice is
worth adding the moment a feature order arrives underspecified often enough to measure;
until then it is a role that will paraphrase the description back.

Whether these profiles reach every worker or only planners is worth deciding explicitly.
Recommendation: only planners, at first. `--add-dir` is unconditional today, so gating it
means `briefing_for()` growing a notion of work-order kind — a small change, but a real
one, and the alternative (every worker can spawn an architect) has an unmeasured cost.

### 5. Review, attention, and who approves a plan

A bad plan is the most expensive failure mode in the system: it spends N worker sessions
before anyone notices. Two decision points follow.

**Plan approval.** Recommendation: **the user approves the plan in v1** — the feature order
goes to `plan_review` and waits. The target state, once plan quality has been observed, is
that **Neo reviews the plan and escalates on doubt**, exactly as it does for privileged
actions: that is what Neo is for, the machinery exists (`neo_store.ask()`, `review()`,
escalation), and it is the only version that keeps the user's attention cost at zero for a
routine feature. Starting with the user is a deliberate confidence-building step, not a
disagreement with the destination.

**Attention rollup.** `true_blockers()` is per work order. A feature order with six
children could put six lines in the "NEEDS YOU" strip, and this codebase already
articulates precisely that fear — the comment on `waiting_pr_merge` in
`project_store.py` says putting every finished work order in that strip "is how that strip
stops being read". Recommendation: `os_status()` groups by `parent_id` and a feature order
contributes **one** line (`fo-1a2b3c — 3/6 done, 1 needs you`), with the children's own
flags intact underneath and reachable on the feature order's page. This is a change to how
attention is *presented*, not to how it is derived; `true_blockers()` stays the single
source of truth.

### 6. Lifecycle

Deliberately not a copy of `WO_STATUSES` — a feature order never runs a session of its own,
so most work-order states are meaningless for it.

```
pending      created; the planner has not been dispatched
planning     the plan work order is running
plan_review  a plan was submitted and is awaiting approval (user, later Neo)
executing    children dispatching / running
completed    every child settled successfully
failed       a child failed and the remainder cannot proceed
cancelled    the user stopped it
```

Rejecting a plan returns the feature order to `planning` with the feedback delivered to
the planner as a message — reusing `jarvis wo send`'s path, so the planner revises in its
existing session rather than starting cold.

### 7. CLI surface

Parallel to `wo`, which is what makes it learnable:

```
jarvis fo create <project> "title" -d "..."      # the coarse ask
jarvis fo list [project] / show <id>             # show renders the tree + per-child status
jarvis fo plan <id> --from-file plan.json        # the planner's terminal action
jarvis fo approve <id> [--reject] [--feedback "..."]
jarvis fo cancel <id>

jarvis wo create ... --parent <fo-id> --depends-on <wo-id,...>
jarvis backlog promote <id> --as feature         # intake -> feature order
```

`jarvis status` gains a feature-order count and the rolled-up attention line; the dashboard
gains a feature page whose main content is the dependency tree.

## Alternatives considered

**One polymorphic `orders` table with `kind` and a self-referencing `parent_id`.** This is
the cleanest expression of what the ask actually describes — "work orders that carry a
semantic definition", with `work_order` as the leaf kind — and it makes a third type nearly
free. It is rejected for now on blast radius, not on taste: `work_orders` is the
most-referenced table in the OS, with `wo_events`, `wo_messages`, `wo_turns`, `assumptions`
and `approvals` all keyed to it, and the state machine, the reconciler, the gate, the
dashboard and the invariants all reading it. Rewriting it polymorphically risks the whole
OS for a generality nothing has yet needed. The trigger that would justify revisiting is a
**third order type that also runs a session of its own** — at that point the two-table
shape starts duplicating, and the rewrite pays for itself.

**Extending the backlog instead of adding a table.** Tempting, because `depends_on`
already lives there. Rejected: the backlog is central and work orders are per-project, so
every parent-to-child traversal becomes a cross-database join with no foreign key, and
backlog items have no lifecycle, no worker, no timeline and no attention — all of which a
feature order needs.

**Planning inside the daemon.** A Python-orchestrated pipeline of `run_headless()` calls,
like `neo.drain_queue()`. Rejected: it is a second execution model to supervise, and it
gives up the worktree, the gate, `jarvis wo ask`, the timeline and cancellation — all of
which the work-order path provides for free. Neo's pipeline is the right shape for
answering a question from stored context; planning needs to read a codebase.

**A generic "order type" plugin abstraction now.** Rejected as premature. Two concrete
types, one of which decomposes into the other, is enough to learn what actually varies.

## Risks and failure modes

- **Plan explosion.** A planner that emits twenty children burns twenty sessions. Cap
  children per feature order (start at eight) and require the planner to justify anything
  above the cap in its submission.
- **Cascading block.** One child fails and its descendants are blocked forever. The feature
  order needs a `failed` path that flags **once**, at feature level, and offers re-plan or
  force-continue — not N stranded `pending` rows nobody looks at.
- **Context starvation between planner and child.** The child worker sees only its own
  description. This project has already recorded the general form of this lesson — the work
  order record must stand alone, because nobody reads worker transcripts — and a feature
  order makes it sharper: the planner must write each child's *full* context into its
  description, including the parts it only knows because it read the whole feature. A plan
  whose children say "as discussed in the plan" is a broken plan, and the submission
  validator should be able to say so.
- **Dependency cycles.** Rejected at plan submission, before anything is created.
- **Cost.** A feature order is N+1 sessions and a bad plan spends all of them. This is the
  argument for the plan review gate being on by default, and for the child cap.
- **Stale plans.** Between plan approval and the last child dispatching, `main` moves. A
  child whose plan assumed a file that no longer exists will discover it mid-session and
  ask Neo. Acceptable for v1; worth measuring before adding anything.

## Migration

Purely additive, and this is worth stating plainly: a new table plus two nullable columns
through the existing `ADDED_COLUMNS` mechanism. No backfill. Every existing work order has
`parent_id NULL` and `depends_on '[]'`, is claimable exactly as before, and behaves
identically. A project that never creates a feature order sees no behavioural change at
all. The only pre-existing code path that changes shape is `claim_next_pending()`, and for
a work order with no dependencies its new `WHERE` clause is vacuously true.

## Phasing

**Phase 0 — prerequisite.** `bl-54287b3f`, auto-complete a work order when its pull request
merges. Ship first; strict dependency satisfaction is unusable without it.

**Phase 1 — dependencies on ordinary work orders.** `work_orders.depends_on`, the claim-time
filter, `jarvis wo create --depends-on`, the blocked-by display. No feature orders, no
planner. This is independently useful the day it lands, and it de-risks the genuinely hard
part — scheduling — before any of the soft part is built on top of it.

**Phase 2 — feature orders with a single generic planner.** The table, the lifecycle, the
plan work order, `jarvis fo plan --from-file`, plan validation, user approval,
`jarvis fo` and the dashboard page. One planner, no profiled team.

**Phase 3 — the team and the rollup.** The three agent profiles shipped through
`install_agent_skills()`, Neo reviewing plans with escalation, feature-level attention
rollup, `max_parallel`.

**Phase 4 — deferred.** Branch stacking (after the base-branch CLI behaviour is verified
live), cross-project programs, and the polymorphic order table if a third session-running
type ever arrives.

## Open questions

1. **Stacked worktrees.** Can `--worktree` create a worktree based on a branch other than
   the default? Everything in option 3 of the dependency rule depends on it, and it has not
   been checked. This should be probed live before Phase 4 is scoped, the same way the two
   facts above were.
2. **Does a child inherit the planner's pull request?** A feature is one logical change; six
   children produce six pull requests. Whether that is right, or whether children should
   stack onto one feature branch, is a workflow question for the user, not a technical one.
3. **Who owns re-planning when a child discovers the plan was wrong?** Today a worker in
   doubt asks Neo. A worker that discovers its *sibling's* work is misconceived has no
   route to say so. This may want a `jarvis fo replan` that a child can call.
4. **Profiles for every worker, or only planners?** Section 4 recommends planners only, but
   the cost of the broader option has not been measured.
5. **Does a feature order need its own gate posture?** A planner reads and writes a plan; it
   never ships. Whether its `permission_mode` and gate configuration should differ from an
   ordinary worker's is a small decision with a security-shaped edge, worth making
   explicitly rather than inheriting.
