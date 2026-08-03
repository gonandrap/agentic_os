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
| The `tools:` key in a seat definition is an **enforced capability restriction**, not advice | a seat declared `tools: Read, Glob` reported `CANNOT-WRITE` and **left no file on disk**; **control**: an otherwise identical seat declared `tools: Read, Glob, Write`, same settings and same permissions, wrote its file |
| `PreToolUse` hooks fire for a **subagent's** tool calls, and the payload identifies the seat | one turn produced two firings; the seat's carried `agent_type: seat-hooked` and an `agent_id`, and the lead's own call carried **no `agent_type` key at all**. `session_id` was identical for both, so `agent_type` is the discriminator |

This matters more than it looks. It means the "team of profiled agents" needs **no new
transport, no new supervision and no change to `worker_session.py`** — agent definitions
ride in beside the skills on the `--add-dir` that every turn already carries. The team is
a content change, not an architecture change.

**The experiment, recorded because the claim rests on it.** A probe agent definition was
written to a scratch directory outside any repository, at
`<scratch>/.claude/agents/jarvis-architect.md`, whose entire system prompt instructed it
to reply with a unique sentinel string. A headless turn was then run from an *empty*
working directory — so nothing local could supply the definition — asking the lead to
invoke that subagent type with the Task tool and report either its exact reply or the word
`UNAVAILABLE`.

- **With `--add-dir <scratch>`**: the lead returned the sentinel, `subtype: success`. The
  Task tool worked under `-p`, and the definition was found.
- **Without the flag, same prompt, same empty cwd**: `UNAVAILABLE`.

The negative control is the load-bearing half. It is what rules out the definition having
been picked up from the ambient environment, and therefore what establishes that
`--add-dir` — the flag Jarvis *already* passes on every turn — is the delivery mechanism.
Everything in section 4, and the "no new framework" position taken in the reconciliation
section below, depends on that control holding. Anyone who doubts it should re-run it
before building on it rather than trusting this table.

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

### 4. The team — four members, decided

**A project's team is exactly four members** (user, 2026-08-02), and the orchestrator
involves the others **on demand, based on the type of order received**. An ordinary work
order engages nobody: the orchestrator dispatches it as it does today, and the team is
never woken. That on-demand rule is what keeps this from taxing the ordinary path.

| Member | Exists today | Is | Engaged when |
|---|---|---|---|
| **orchestrator** | **yes** | the project's daemon-side dispatcher | always |
| **planner** | no | a work order, in a worktree | a feature order arrives |
| **architect** | no | a subagent seat of the planner | the planner asks |
| **test lead** | no | a subagent seat of the planner | the planner asks |

Only two of the four are new *agents*, and both are seats inside the planner's session.
The orchestrator already exists; the planner is an ordinary work order with a different
briefing. Nothing here adds a supervised process.

**The orchestrator carries the scope mandate.** The roster has no separate scope seat, and
it does not need one: the orchestrator received the feature order, so it holds the original
ask verbatim, and it does not participate in the planning deliberation. That makes it
**structurally blind** rather than blind by configuration — the stronger form of the
property, obtained for free. Its scope check is a single question asked of the returned
plan before anything is accepted: *is this what was requested?*

This is where PR 64's blind-seat argument lands in this design. Independence is what turns
agreement into evidence rather than an echo, and a scope check that has already read the
decomposition will rationalise it — every child looks necessary when it is judged against
the plan instead of against the ask. The architect and the test lead are deliberately
**sighted**: an architect that cannot see the codebase cannot decompose anything, and a
test lead that cannot see the decomposition cannot write criteria against it. So the
general rule for this roster is that **a member is sighted when its job needs the artefact,
and blind when its job is to check the artefact against something outside it** — and the
only member whose job is the latter is the one that never entered the room.

Mechanically, for the two seats: `src/jarvis/assets/agents/*.md`, materialized by
`install_agent_skills()`
into `.jarvis/agent-skills/.claude/agents/`, reaching the worker on the `--add-dir` that
`briefing_for()` already passes. Verified above. The function is already documented as
owning and rebuilding its whole generated tree, so adding a sibling directory is a small,
well-precedented change — likely a rename to something like `install_agent_assets()`.

`src/jarvis/assets/agents/` is one of the two surfaces shared with the Neo panel design;
see the reconciliation section below. The authoring format is common, the rosters are not.

The two seats:

- **architect** — decomposition and sequencing. Which pieces are separable, what the
  interface between them is, what must land first. **Sighted**: reads the codebase, and
  sees the planner's framing.
- **test lead** — what "done" means for each child. Every child needs acceptance criteria
  in its own description, because the child worker will never see the plan. **Sighted**:
  acceptance criteria have to be written against the architect's actual decomposition.

"Product manager" and "project manager" from the original sketch are deliberately absent,
and this survives the user's roster unchanged. The planner *is* the project manager —
sequencing and slot budgeting are its own job, and a separate PM agent holds no information
the planner does not.

**Ordinary workers get no profile** (user, 2026-08-02): a worker is an individual that gets
the work done, and a role would be dressing up a session with exactly one job. So the seats
reach planners only, which means `briefing_for()` grows a notion of work-order kind —
`--add-dir` is unconditional today. That is a small change, and it is the whole cost of
keeping the ordinary path untouched.

### 5. Review, attention, and who approves a plan

A bad plan is the most expensive failure mode in the system: it spends N worker sessions
before anyone notices. Two decision points follow.

**Plan approval: Neo reviews the plan and escalates on doubt — from v1** (decided
2026-08-02). The feature order enters `plan_review`, Neo reads the submitted plan exactly
as it reads a privileged-action request, and either releases it to `executing` or escalates
to the user. The machinery already exists: `neo_store.ask()`, `review()`, and the
escalation path that makes a decision the user's only when Neo declines to take it.

An earlier draft of this document recommended the *user* approving every plan in v1, as a
confidence-building step. That is reversed, and the reason is the principle the user stated
directly: **no routine attention unless something is off.** A user who does not want to
review each implementation milestone is not going to want to hand-approve each plan either,
and a feature order that costs an interactive review every time is a feature order that
costs more attention than typing six `jarvis wo create` calls — which is the thing this
design exists to replace.

**That reversal moves weight onto two backstops, so both are load-bearing rather than
nice-to-have, and neither may be dropped as a simplification:**

1. **The child cap.** A hard ceiling on children per feature order — start at eight. Above
   it the planner must justify the count in its submission, and the plan does not validate
   without that justification. This is what bounds the blast radius of a plan Neo waves
   through: the worst case is a bounded number of wasted sessions, not an unbounded one.
2. **The plan validator.** Structural rejection at submission, before anything is created:
   dependency cycles, unknown or dangling ids, children over the cap without justification,
   and — the one that matters most in practice — **children whose description does not stand
   alone.** A child that says "as discussed in the plan" is rejected, because the child
   worker will never see the plan. These are mechanical checks in Python, not judgement, and
   they run whether or not Neo is paying attention.

The escalation triggers are worth naming explicitly rather than leaving to Neo's discretion:
a plan at or over the child cap, a plan whose children touch a project's gated actions, and
any plan Neo cannot reconcile with a standing learning. Those are the cases where the
attention is not routine, which is exactly when the user should see it.

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
plan_review  a plan was submitted; Neo is reviewing it, or it is escalated
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

### 8. Per-seat capability posture — a planner plans, it does not build

**The user's requirement (2026-08-02):** a planner's job is to produce a plan others work
from, *never* to do the work itself. A planner that returns the built solution has failed,
and that output must not be pushable. The same applies inside the team: the architect must
not produce code either. Every member has its own posture.

This is not a matter of instructions. A prompt saying "do not write code" is a preference
the model can talk itself out of at hour three of a hard plan. The posture has to be
enforced, and the two probes above establish that there are **two independent enforcement
layers**, with different properties:

**Layer 1 — declarative capability restriction, in the seat definition.** The `tools:` key
of an agent definition is enforced by the CLI, not advisory: the restricted probe seat
reported `CANNOT-WRITE` *and left no file on disk*, against a control seat that differed
only in that key and wrote successfully. So an architect that cannot produce code is
expressed by not granting it `Write`, `Edit` or `NotebookEdit` — one line of frontmatter,
no Jarvis code, no round trip, and no way for the model to route around it.

**Layer 2 — gate mediation, through the existing `PreToolUse` path.** Hooks fire for a
subagent's tool calls, and the payload carries `agent_type` (the seat's name) and
`agent_id`, while the lead's own calls carry **no `agent_type` key at all**. `session_id`
is shared, so `agent_type` is the discriminator. This means per-seat gating needs a new
*field* in `gates.py`'s matching, not a new mechanism: the machinery already runs on every
tool call in a worker turn and already travels as `JARVIS_GATES` in the worker settings.

**Recommendation: prefer layer 1 for prohibitions, and reserve layer 2 for actions that
should be possible but reviewed.** A prohibition routed through layer 2 costs a Neo round
trip to say "no" to something that was never allowed, and it puts a permanent false
positive into the gate false-positive rate that the `dismissed` verdict exists to measure.
So:

| Member | Layer 1 — tools withheld | Layer 2 — gated |
|---|---|---|
| planner (a work order) | `Write`/`Edit` outside its plan artefact | the existing `pr_merge` / `release` / `service_restart` set |
| architect | all edit tools | — |
| test lead | all edit tools | — |

The planner is the one member that cannot be handled by layer 1 alone: it is a work order,
not a subagent, so it has no `tools:` frontmatter. Its restriction is the per-work-order
settings path `dispatch._write_worker_settings()` already writes — the same declarative
`permissions.deny` mechanism that grants an ordinary worker edit rights inside its own
worktree, inverted. That is a Jarvis-side control rather than a CLI-side one, which is
worth stating plainly: it is the weaker of the two layers, and it is the one place where
"a planner must not build" rests on configuration Jarvis writes rather than on a capability
the CLI never granted.

**One caveat for the implementer.** `JARVIS_WO_ID` is per-session, so an action a *seat*
attempts and a gate blocks files its approval request against the **planner's** work order.
That is correct — the planner owns the turn and is answerable for what its team did — but
anyone reading the resulting request needs `agent_type` recorded on it, or the record will
say the planner attempted something the architect did.

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
answering a question from stored context; planning needs to read a codebase. This is the
rejection the reconciliation below turns on, and it was reached before the Neo panel
design was known — two designs arriving at it independently is part of why it stands.

**A generic "order type" plugin abstraction now.** Rejected as premature. Two concrete
types, one of which decomposes into the other, is enough to learn what actually varies.

## Reconciliation with the Neo panel design (PR 64)

`docs/superpowers/specs/2026-08-02-neo-team-design.md`, from work order `wo-18c2e7e4`,
replaces Neo the single decider with a panel of profiled agents. It carries a section
proposing a shared `panel.py` primitive, with this design named as its intended second
caller, and leaves ownership to be settled between the two. This section is the answer,
and it is symmetric: neither document proceeds on an assumption about the other.

**Both designs deliberate with a roster of profiled agents. They should not share a
mechanism, because they are opposite on every axis that decides one.**

| | Neo panel | Feature-order planner |
|---|---|---|
| Orchestrated by | Python, in the daemon | the lead model, via the Task tool |
| Runs in | the daemon process, fanning out `run_headless()` | a work order — a real session, in a worktree |
| Seats see | a question plus stored context | the codebase |
| Blindness | mandatory: agreement is only evidence if independent | seats are sighted; the blind check sits with the orchestrator, outside the room |
| Seats are | review lenses | domain roles, which the panel design explicitly declines |
| Rounds | one blind round, then a chair | iterative; the lead consults, reads the answer, consults again |
| Output | one strict-JSON verdict | a validated dependency graph |

The load-bearing row is the third. An architect seat that cannot open a file cannot
decompose a feature, and the panel's seats have no worktree by construction. Adopting
`panel.decide()` would mean moving planning into the daemon — the alternative rejected
immediately above, on grounds that have nothing to do with this reconciliation.

**Ownership: `panel.py` belongs to `wo-18c2e7e4`, outright.** This design does not claim
it, does not call it, and does not want a share of it. Neo is the only consumer that needs
blind Python fan-out; planning stays session-side. The concern the shared-primitive
proposal was guarding against — the OS acquiring two bodies of multi-agent orchestration
code that drift apart — turns out not to arise here, because *this design contributes no
orchestration code at all*. Its team is markdown under `assets/`, delivered by the
`--add-dir` path that `bootstrap.install_agent_skills()` and `worker_session.briefing_for()`
already maintain, as the negative-control experiment above establishes. There is no second
framework to diverge from the first.

**What is shared is exactly two surfaces:**

1. **One seat-definition authoring format and directory**, `src/jarvis/assets/agents/`, in
   the Claude Code agent-definition markdown format. The panel reads those files to build
   its per-seat system prompts; this design's planner receives them for free over
   `--add-dir`. **Two loaders, one authoring format, different rosters** — a seat's mandate
   is written and edited in one place regardless of which caller runs it.
2. **One strict-structured-output validate-and-retry helper**, generalised out of
   `neo.parse_verdict`. The panel needs it for the chair's verdict; this design needs it
   for plan submission, which is a graph that must be validated for cycles, unknown ids and
   child count before anything is created.

**Whichever work order lands first extracts the shared helper and the
`src/jarvis/assets/agents/` layout; the second adopts it rather than forking it.** The same
sentence appears in PR 64.

Neither work order has implemented anything at the time of writing, which is why this was
settled between two documents rather than between two merged modules.

## Risks and failure modes

- **Plan explosion.** A planner that emits twenty children burns twenty sessions. Cap
  children per feature order (start at eight) and require the planner to justify anything
  above the cap in its submission — and escalate any plan at or over the cap to the user
  rather than letting Neo release it. See section 5: this cap is one of the two backstops
  the Neo-reviews-plans default rests on.
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
  argument for the child cap and the plan validator being load-bearing rather than
  optional, now that Neo rather than the user is the routine reviewer (section 5).
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

**Phase 0 — prerequisite. DONE.** `bl-54287b3f` was promoted while this design was in
review and shipped as `wo-6722430e` / PR 66, already merged to `main`. It landed stronger
than this document assumed: a new `src/jarvis/github.py` (read-only, `gh pr view <url>
--json state,mergedAt`) plus `Daemon.poll_pull_requests` on a `PR_POLL_EVERY_TICKS`
cadence of roughly two minutes. MERGED becomes `ops.complete_merged` — `completed`, event
`pr_merged`, backlog item closed, worker stopped; CLOSED-unmerged becomes `needs_review`
with attention.

The consequence for this design is direct and good: **advancing the schedule no longer
costs the user a `jarvis wo done` per child.** They merge the pull request — which they
were going to do anyway — and the dependency clears within about two minutes, unattended.
The strongest objection to strict dependency satisfaction, as originally written here, no
longer exists.

**Phase 1 — dependencies on ordinary work orders, and the stacking probe.**
`work_orders.depends_on`, the claim-time filter, `jarvis wo create --depends-on`, the
blocked-by display. No feature orders, no planner. This is independently useful the day it
lands, and it de-risks the genuinely hard part — scheduling — before any of the soft part
is built on top of it.

**Scoped into this phase by the user (2026-08-02): establish whether stacked worktrees are
possible, first, before the rest of the phase is designed around an answer.** Concretely,
a live probe with a negative control in the style of the two above: can a worktree be
created from a base branch other than the default — via `--worktree`, or by creating the
worktree with `git` directly and running the turn with `cwd` set to it and no flag (the
transport already does exactly this from turn 2 onward, so the second form is very likely
to work even if the first does not). The probe's deliverable is a yes/no plus the working
invocation, and it gates nothing else in Phase 1 — the `depends_on` column and the
claim-time filter are the same either way. If stacking proves impossible, the alternatives
to evaluate are: serialise on merge (strict, the pre-Phase-0 behaviour), or have the child
worker rebase onto its dependency's branch itself as its first act, which is slower and
puts a merge conflict inside a worker session rather than in front of the user.

**Phase 2 — feature orders with a single generic planner.** The table, the lifecycle, the
plan work order, `jarvis fo plan --from-file`, the plan validator and the child cap,
Neo reviewing plans with escalation, `jarvis fo` and the dashboard page. One planner, no
seats yet — the orchestrator's scope check is the only review of the plan's shape, which
is why the validator lands in this phase and not later.

**Phase 3 — the seats and the rollup.** The architect and test-lead seats shipped through
`install_agent_skills()`, their `tools:` postures per section 8, feature-level attention
rollup, `max_parallel`. This is the phase that touches the two surfaces shared with PR 64:
if the Neo panel has landed first, adopt its `src/jarvis/assets/agents/` layout and its
structured-output helper rather than forking them; if it has not, extract both here in a
shape the panel can adopt.

**Phase 4 — deferred.** Branch stacking (after the base-branch CLI behaviour is verified
live), cross-project programs, and the polymorphic order table if a third session-running
type ever arrives.

## Decisions

The five questions this document originally left open were answered by the user on
2026-08-02. They are recorded here as decisions rather than deleted, because the reasoning
matters more than the conclusion and a later reader will want to know what was considered.

**1. Stacked worktrees — probe it in Phase 1, then evaluate.** Not deferred to Phase 4.
Establish empirically whether a worktree can be based on a branch other than the default;
if it cannot, evaluate the alternatives rather than assuming the design is stuck. Folded
into the phasing above, with the two named alternatives.

**2. Children stack their pull requests — "one PR on top of each other".** Each child still
opens its own reviewable pull request; the base of each is its dependency's branch rather
than the default branch. This chooses option 3 of the dependency rule, which is why
decision 1 moved forward into Phase 1: the whole scheme rests on being able to cut a
worktree from a non-default base. When the planner itself produced a pull request (a design
document, say), that PR is the base of the stack; otherwise the base is the default branch.

**3. A child never judges its sibling's work — it raises to Neo, which decides.** The
original question assumed a child might call something like `jarvis fo replan`. That was
the wrong shape: a work order does not have enough context to claim a feature needs
re-planning, and evaluating a sibling is not its job. If a child notices a problem *by
chance*, it does what every worker already does with anything it is unsure about — asks
Neo — and Neo weighs the severity and decides whether a re-plan is warranted. **No new
verb, no new permission, no cross-work-order authority.** The existing first-responder path
already covers it, which makes this a decision to build nothing.

**4. Ordinary workers get no profile.** Profiles belong to the planning team only. A worker
is an individual that gets the work done; giving it a role would be dressing up a session
that has exactly one job. This settles section 4's open recommendation in favour of the
narrower option, and it means `briefing_for()` needs a notion of work-order kind only for
planners — nothing changes for the workers that make up the overwhelming majority of the
fleet.

**5. Yes — and per member, not just per feature order.** Answered at length in section 8
above, with the two enforcement layers the probes established. The principle: a planner
produces a plan others work from and never the work itself, and each member of the team
carries its own posture — an architect cannot produce code any more than the planner can.
