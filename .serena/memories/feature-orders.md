# Feature orders — the planned unit of work above the work order

A coarse ask the OS decomposes ITSELF, instead of the user splitting it in chat and
typing six `jarvis wo create` calls. Design:
`docs/superpowers/specs/2026-08-02-feature-orders-design.md`. Work-order machinery it
sits on: `mem:work-order-lifecycle`.

**Phases 0, 1, 2 and 3 have shipped. Phase 4 (branch stacking) has not** — it is in the
backlog (`bl-e3a88979`) with the verified stacking invocation attached; read it before
designing anything there.

## The loop

```
jarvis fo create <project> "title" -d "the whole ask"    # -d is REQUIRED
  -> Daemon.plan_features  creates ONE planner work order      fo: pending -> planning
  -> the planner reads the codebase, writes plan.json
  -> jarvis fo plan <fo-id> --from-file plan.json              fo: -> plan_review
       validates (plans.parse_plan), stores, queues a Neo review,
       and SETTLES THE PLANNER — `fo plan` IS its `jarvis wo finish`
  -> Neo reviews (kind='plan')
       release  -> children created with their edges              fo: -> executing
       reject   -> reason delivered to the planner as a message   fo: -> planning
       escalate -> attention on the FEATURE ORDER; `jarvis fo approve`
  -> Daemon.settle_features                                  fo: -> completed | failed
```

Nothing else schedules anything. Children are ordinary work orders with Phase 1
`depends_on` edges, and `claim_next_pending`'s existing `WHERE` clause dispatches them in
order. **No new status, no new dispatch path, no second scheduler.**

## Data model

`feature_orders`, in the PER-PROJECT db next to `work_orders` (`project_store.py`):
`plan_wo_id` (the planner), `plan` (the submitted document, JSON), `plan_question_id`
(back-link to Neo's review question — mirrors `approvals.neo_question_id`, because Neo's
db is OS-wide and knows nothing about a project's tables), `max_parallel` (Phase 3, unused).

On `work_orders`, through the existing `ADDED_COLUMNS` migration: `parent_id`
(REFERENCES feature_orders) and `kind` (`WO_KINDS` = worker | planner). Purely additive —
every pre-existing row is `parent_id NULL`, `kind='worker'`, and behaves identically.

`kind` is a column rather than a derivation because `parent_id` cannot tell a planner
from a child (both carry it), and `briefing_for`/`build_worker_prompt` must know which
contract to compose without a second table lookup on every dispatch.
`store.feature_children()` returns only `kind='worker'` — the planner belongs to the
feature order but is not a piece of the work.

`FO_STATUSES`: pending, planning, plan_review, executing, completed, failed, cancelled.
Deliberately NOT a copy of `WO_STATUSES` — a feature order never runs a session.

A feature order has **no timeline table**. Its history is written into the timeline of
whichever work order carried the step (`plan_submitted` / `plan_reviewed` on the planner,
`created` on each child), which is where anyone investigating is already looking.

## `plans.py` — the validator, and why it is load-bearing

Neo, not the user, is the routine reviewer of plans. That reversal rests on two backstops
the design calls load-bearing rather than optional, and **neither may be dropped as a
simplification**: the child cap, and this validator. Pure functions, no LLM, no database
— the checker has to be more trustworthy than the thing it checks. Four rejections:

1. **Cycles** (`find_cycles`, DFS so the error names the ring). Phase 1's live edges are
   acyclic BY CONSTRUCTION — an edge may only point at a row that already exists — but a
   plan is written before any row exists, so that argument does not reach it. This is the
   real cycle check Phase 1's `create_work_order` docstring says is owed.
2. **Unknown ids** — a `needs` naming nothing in the plan; the failure that most looks
   like success, since it would land as a child with no edge at all.
3. **The child cap** (`CHILD_CAP = 8`). Over it, the plan must carry a `justification`
   or it does not validate.
4. **Descriptions that do not stand alone** — the one that matters most in practice. The
   child worker sees its own description and NOTHING else. `MIN_DESCRIPTION_CHARS = 80`,
   no description that merely repeats the title, and a curated `DANGLING_PHRASES` list.

**A trap already hit once in that phrase list:** ordinals are NOT outward references.
"The first step is to add the column" is how a good standalone brief opens, and rejecting
it teaches planners to write worse prose to get past the checker — which costs exactly
the context the check exists to protect. `test_plan_validator.py` keeps negative controls
next to every rejection for this reason.

`creation_order()` is not tidiness: children must be created in dependency order so their
edges go through the same guarded `create_work_order` path a hand-typed `--depends-on`
does. Stamping edges onto rows afterwards is the exact move that loses acyclic-by-
construction. `ProjectStore.create_plan_children` does it in one transaction.

## Neo reviewing a plan

`plan` is a third `neo_store.Q_KINDS`, with `plans.PLAN_REVIEWER_PERSONA`. **Deliberately
NOT routed through the approvals/gate table**: that table is a receipt for one command
string, and its `dismissed` count is the OS's classifier false-positive rate — a real
metric that plan reviews would corrupt. `questions.wo_id` holds the PLANNER's work order.

The reviewer is **structurally blind**: Neo was not in the planning session, holds the
user's ask, and reads the decomposition cold. A scope check that has followed the
reasoning rationalises it — every child looks necessary when judged against the plan
instead of against the ask.

**THE CAP OVERRIDES NEO** (`Daemon._deliver_plan_verdict`). A plan at or over
`CHILD_CAP` escalates whatever Neo said: a backstop the reviewer can wave through is not
one. Neo is still ASKED, and its reading is folded into the reason the user sees —
skipping the call would hand them a nine-node graph with no read on it. The question is
re-`mark`ed `escalated` so `jarvis neo list` and `jarvis status` agree.

`ops.review_plan` is ONE function for both deciders. An escalation means Neo declined to
take the decision, not that the decision changed shape; two implementations of "release
the plan" would be two chances to disagree about what releasing means. It refuses a
rejection with no feedback, and it no-ops if the user got there first.

## Settling (`Daemon.settle_features`)

* `completed` when EVERY child is `completed`. `waiting_pr_merge` does not count — same
  strict rule as Phase 1's edges: a feature is done when its code is on the default
  branch. The merge poller closes each child ~2min after the user merges.
* `failed` when ANY child is `failed` or `cancelled`. **Without** the design's "and the
  remainder cannot proceed" qualifier (ruled 2026-08-03): a feature with a dead child
  needs a human whichever siblings could still run, so the reachability check buys
  nothing and is easy to get subtly wrong. A cancelled child counts, but the reason says
  cancellation — `invariants.FEATURE_CHILD_FAILED` vs `FEATURE_CHILD_CANCELLED`, because
  a failure is a problem to diagnose and a cancellation is a decision already taken.
* **Flag-once is true BY CONSTRUCTION**, not by bookkeeping: only `executing` feature
  orders are examined, and a failing one leaves `executing` in the same call that raises
  its flag. No dedupe set.
* **No notification.** A failed child already pinged the user via `settle_work_order`,
  and `notify.route_new_inbox` applies no level filter — every inbox row reaches every
  sink — so a second row is the same event arriving on the phone twice.

Feature-order attention is NOT re-derived by `invariants.true_blockers` (that function
answers "what does this WORK ORDER need from me"), so INV-ATTENTION-REASON cannot
relabel these reasons. If a feature-order invariant is ever added it inherits the
obligation: whatever derives the flag must be able to produce those strings.

## The team (Phase 3) — two seats, and the posture that makes them safe

`src/jarvis/assets/agents/jarvis-architect.md` and `jarvis-test-lead.md`, in Claude Code's
agent-definition markdown. `bootstrap.install_agent_assets(path, kind)` — renamed from
`install_agent_skills` — materialises them into `.jarvis/agent-seats/.claude/agents/` and
returns the `--add-dir` roots for that kind; `worker_session.briefing_for` passes
`wo["kind"]`, which is the whole of how "seats reach planners only" (decision 4) is
enforced. `dispatch._planner_prompt` names both seats and when to consult them: two
definitions nothing ever invokes are two files on disk.

**TWO ROOTS, not one, and it is not tidiness.** Skills stay at `.jarvis/agent-skills/`
for every worker; the seats get their own sibling root. A single root would force the
worker path to DELETE `agents/` to keep owning its whole generated tree — and that delete
lands while a concurrently dispatched planner is reading it.

**`tools: Read, Grep, Glob` — and deliberately no `Bash`** (ruled 2026-08-03). The design
says only "all edit tools withheld", but withholding `Write` while granting a shell is not
a prohibition: `cat > f <<EOF` writes the file just as well. The `tools:` key is enforced
by the CLI, not advisory (probe: a `Read, Glob` seat reported CANNOT-WRITE *and left no
file on disk*, against a control differing only in that key), which is why layer 1 is the
one that counts. `test_feature_order_team.py` asserts against the shipped frontmatter, not
a Python constant — the file the CLI reads IS the enforcement.

**The planner itself keeps ORDINARY worker permissions**, unchanged in Phase 3 and pinned
by `test_a_planner_runs_on_ordinary_worker_permissions`. It is a work order, not a
subagent, so it has no `tools:` frontmatter; the only Jarvis-side lever is a settings
`permissions.deny`, and a deny broad enough to stop product code also stops the two things
a planner MUST do — write the `plan.json` it submits, and produce a design document whose
PR the design makes the base of the children's stack (decision 2). Phase 3 considered
narrowing it to the project's source directories and did not: "source directory" is not a
concept the catalog has, so it would mean guessing `src/`-shaped paths per project and
breaking exactly the design-document case. So "you plan, you do not build" stays prose in
`_planner_prompt`, and the enforced posture lives on the seats.

**The seats navigate by SYMBOL INDEX, not by grep**, and three live probes decide how
that is wired. (1) `tools:` excludes MCP tools by default — a seat declared
`Read, Grep, Glob` reported SERENA-UNAVAILABLE, so the Serena names are listed explicitly.
(2) **Availability is not permission**: a seat holding `activate_project` in `tools:` but
not in `permissions.allow` had the call BLOCKED and gave up, so
`dispatch.serena_allow_rules()` writes an allow rule for every read-only Serena tool into
the worker settings. (3) An unknown tool name in `tools:` is inert, so BOTH
`mcp__serena__` and `mcp__plugin_serena_serena__` are always listed — Jarvis configures no
MCP server itself and cannot know which install it is on. **Never grant Serena
wholesale**: it ships `execute_shell_command`, `create_text_file` and
`replace_symbol_body`, which would hand a shell to the seats that were deliberately denied
one. The same ranking is in `build_worker_prompt` and `OPERATION.md` (v8) for the whole
fleet, stated conditionally. Graded by `evals/llm/test_navigation_judgment.py`, which
watches TOOL CALLS via a `PreToolUse` recorder and attributes them by `agent_type`.

**`agent_type` on `approvals`** (Phase 3, layer 2). `JARVIS_WO_ID` is per-session, so a
gate a SEAT trips files against the work order that owns the turn — correct ownership, but
without the column the record says the planner attempted what the architect did.
`PreToolUse` carries `agent_type` for a subagent's call and omits the key entirely for the
lead's, so absence is the discriminator. Built even though no seat can currently reach a
gate (they have no `Bash`): the day anyone grants one, the trail is already right.

## `max_parallel` — the user's knob, not the planner's

`jarvis fo create --max-parallel N` (also on `backlog promote --as feature`), NULL meaning
uncapped. Enforced by a second `NOT EXISTS` in `ProjectStore.claim_next_pending`, spent
ALONGSIDE the project's `max_concurrent` rather than instead of it — whichever is tighter
binds. Vacuously true for `parent_id IS NULL`, so the ordinary path is the query it always
was. Design section 4 calls slot budgeting the planner's job; that was reversed
(2026-08-03) because a planner that budgets its own slots can hand itself the whole
project's concurrency, and it would be one more thing `plans.parse_plan` has to police.

The PLANNER is exempt from its own feature's cap (`w.kind = 'worker'` inside the clause):
capping the session that decides what the children are against those children is capping a
feature against itself. A held child says so through `invariants.status_label` — the single
funnel every surface renders through — as `pending — waiting for a slot in fo-… (2/2
children running)`, ranked BELOW the dependency label, because a child waiting on a
sibling's merge will not start when a slot frees. It raises no attention: a slot always
frees, so this is the system working.

## The attention rollup

`os_status` groups flagged work orders by `parent_id`; a feature order contributes ONE
line (`1/3 done — 2 of its work orders need you: …`) and its children's lines are
suppressed. Presentation only — `true_blockers` stays the single source of truth, nothing
is cleared, and `jarvis wo list` still shows every flagged child. The link goes to the
feature page, which is what makes collapsing safe.

**Two Phase 2 bugs this uncovered, both fixed here.** `os_status` scanned only
`FO_OPEN_STATUSES` for feature attention — but `failed` is a SETTLED status and is also
the only one a feature order raises its own flag in, so the flag was derived and never
shown (`store.flagged_feature_orders()` now backstops it). And `jarvis status` read
`approval_id` off every attention item carrying a `decide` key, which a feature order does
not have, so the line raised `KeyError` outright.

## Surfaces

`jarvis fo create|list|show|plan|approve|cancel`, `jarvis backlog promote --as feature`,
and `/fo/{project}/{fo_id}` in the dashboard — the one view holding the ask, the plan as
submitted, and each child's live status at once, which is also why an escalated plan is
decided there. The project page lists feature orders but does NOT expand their children:
those are already in the work-order listing below, and printing the tree twice on one
page is how a page stops being read.

## The question diet (wo-e4a359cb, 2026-08-09) — skeleton reviews and the design doc

Production plan-review questions #65–#67 hit 67–84KB because `build_plan_question`
inlined every child's full brief; measured on the real #67 the skeleton cut the
question from 21,250 to 1,999 input tokens (−90.6%). Four rules now hold:

* **`build_plan_question` renders a SKELETON** — ask verbatim, summary, one line per
  child (key/title/needs + `done when:`), `design_doc` name — never descriptions.
  `render_plan` (full) still backs `jarvis fo show` and the dashboard. TEST TRAP: the
  fake Neo keys FORCE_* markers on the QUESTION text, so test plans must carry markers
  in the SUMMARY (see `a_plan` in `tests/test_feature_orders.py`), not in briefs.
* **`plan.json` has a first-class `design_doc`** (relative path, no `..`, validated in
  `parse_plan`; file must exist at submit or `fo plan` refuses). `ops.submit_plan`
  snapshots its text into the stored plan (`design_doc_content`);
  `dispatch.materialize_design_doc` writes it to
  `<project>/.jarvis/features/<fo-id>/<basename>` at child dispatch and the child
  prompt gets a `# Design document` section pointing at it. Children cannot see the
  planner's unmerged branch — that is why a snapshot, not a git reference. The planner
  contract now says: shared context goes in the doc ONCE; a brief is goal, scope
  boundary, acceptance, plus section references. "Repetition is cheap" is gone.
* **A question to Neo is one paragraph** that may reference an artifact in-text —
  `section 3 of design doc "docs/x.md"` (`sections.find_refs` / `extract_section`).
  `ops.ask_question` resolves it (worktree → project tree → feature snapshot) and puts
  ONLY that section in the question's `context`; unresolved references append a note
  rather than failing the ask. `sections.QUESTION_WARN_CHARS=1500` warns,
  `QUESTION_MAX_CHARS=4000` refuses with the fix named.
* **Escalation inbox rows are headlines**: first line of the question, 200 chars, plus
  `jarvis neo show/answer` pointers (`daemon.py` question-escalation branch).
  `PLAN_REVIEWER_PERSONA` no longer tells Neo to judge briefs it does not receive.

`evals/test_question_diet_budget.py` pins the ratio on a production-shaped synthetic
plan and prints the readings from the module fixture's teardown.

The other half of the diet's bargain is graded: `evals/llm/test_plan_review_judgment.py`
(opt-in, JARVIS_EVALS_LLM=1) runs seven skeleton questions through the REAL
`kind="plan"` path — release-blocked recall, escalation choice, release willingness,
reason quality — and `tests/test_plan_review_eval_harness.py` keeps it honest for free
(gate spelling, battery skeleton-shaped by construction, canned-model smoke of the
plumbing). CALIBRATION FACT the scope-gap scenario encodes: an ask half-covered by a
plan is a REJECT (planner's call) when re-decomposing fits under the child cap, and an
ESCALATE only when folding the missing half in would exceed it — Neo graded the
3-child version of that fixture correctly against the eval's own expectation.
