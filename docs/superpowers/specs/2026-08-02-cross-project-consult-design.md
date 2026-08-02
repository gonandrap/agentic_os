# Cross-project consult — letting one project's work draw on another's

*2026-08-02*

Design only. Nothing in this document is implemented; it exists to be argued with.

## 1. Problem

The request, as filed:

> As each project evolves, I'm thinking that one way to reuse learnings from one project
> into another is to let orchestrators from one project talk to orchestrator from another
> project. So for example, if I say "project A should implement coordination as project B
> did" then when project A orchestrator gets that work order, it will note that and send a
> question to orchestrator of project B to ask how they implemented coordination in project
> B. To accomplish that messaging, I'm thinking that project orchestrators may need to be
> part of a claude team, so they can talk to each other directly without the need of
> work_orders between them (although it would be good to have traceability that they talked
> each other, so I know what's going on).

Today a work order in project A is answered entirely out of project A. Everything the
worker knows about the fleet arrives through one channel: the knowledge base rows injected
into its prompt. If the useful precedent lives in project B's source, its git history, or a
learning some B worker wrote six weeks ago, A's worker will re-derive it — differently, and
usually worse.

## 2. What the OS actually has today

The proposal assumes a per-project orchestrator agent that can hold a conversation. That
agent does not exist, and its absence is deliberate rather than accidental — `ASSUMPTIONS.md`
§A.1:

> **The per-project "orchestrator background agent" is a deterministic daemon, not a Claude
> agent.** […] Since that role needs zero intelligence (poll DB, spawn, track), implementing
> it as an LLM session would burn tokens idling and be less reliable. […] If you want a
> literal Claude orchestrator per project, the dispatch layer is abstracted so it can be
> swapped.

So the entities that could conceivably talk to each other are:

| Entity | Lifetime | Knows about | Can read code? |
|---|---|---|---|
| `jarvisd` (the "project orchestrator") | forever | queue state, no semantics | no — it is Python, not an agent |
| Worker | one work order | its own WO + injected knowledge | yes, in its own worktree |
| Neo | one headless call per question | the user's learnings, all projects | no repo access |
| Jarvis | the user's chat session | whatever the CLI reports | yes, wherever the user is |

There is no long-lived per-project agent to address a message to. Building the proposal
literally means creating one first — see Option D.

### Platform reality (Claude Code 2.1.220, the version this OS runs against)

Checked against the installed binary while writing this:

| Claim | Finding |
|---|---|
| A `teams` concept exists in the CLI | No. Subcommands are `agents`, `auth`, `auto-mode`, `doctor`, `gateway`, `install`, `mcp`, `plugin`, `project`, `setup-token`, `ultrareview`, `update`. |
| Two independent top-level sessions can message each other | No supported channel. `claude agents` starts, lists and stops background agents; it has no send verb. |
| `--agents <json>` provides peer messaging | No — it defines *subagents* a session may spawn. Messaging there is parent→child inside one session, not peer-to-peer across projects. |
| Headless turns appear in the agents roster | No (`docs/superpowers/specs/2026-08-01-headless-turn-runtime-design.md`) — workers are `claude -p` processes Jarvis owns end to end. |

This matters more than it looks. Whatever "a Claude team" would mean, **Jarvis would have to
build the transport itself** — and once Jarvis owns the transport, the team framing adds no
capability over the options below, only cost.

### Cross-project knowledge: what already works, and one asymmetry

`central_store.py` gives the `knowledge` table a `project` column where `''` means global.
Two readers exist and they disagree in scope:

* `search_knowledge(term)` — **unscoped**. `jarvis learn search <term>` already searches
  every project's learnings today. This is the one cross-project affordance that exists.
* `relevant_knowledge(project, limit)` — `WHERE project=? OR project=''`, ordered by
  recency. This is what `dispatch.dispatch_work_order` injects into the worker prompt.

The prompt section that carries it is headed (`dispatch.py:172`):

```
# Knowledge base (learnings from this and other projects)
```

That header is not true of its contents. A worker is told it is looking at other projects'
learnings while looking at its own plus global ones. The worker therefore has no reason to
go searching for B — it believes B is already in front of it. **This is the cheapest single
fix in the whole document** and it is a prerequisite for everything else (§6, Stage 0).

### Adjacent work already in flight

Two open PRs bear directly on this and neither is merged into `main`:

* **PR 27 — "Knowledge base: ship workers an index they query, not a payload."** Replaces the
  injected payload with a bounded index plus retrieval verbs (`jarvis learn search|show|list|topics`)
  and adds a READ bullet to the worker contract. This is the retrieval substrate a consult
  design should build on rather than duplicate.
* **PR 57 — "Cap Neo's answers, and let it file its own ledger cleanups."** Introduces work
  orders with `origin='neo'` and a pre-approval marker in `work_orders.metadata`: the existing
  precedent for *one agent filing work for another*, including the guards that stops it
  recursing. Option C below is the same shape and should inherit those guards.

## 3. Separating the requirement from the proposed mechanism

The work order proposes a mechanism ("a Claude team") in service of a goal. Stated as
requirements the goal is:

* **R1 — Discovery.** A worker in A can find out that B has solved this before.
* **R2 — Depth.** The answer is grounded in B's actual code, history and decisions, not only
  in whatever one-line learning someone happened to write down.
* **R3 — Latency.** A does not stall for hours waiting.
* **R4 — Traceability.** The user can see that A asked B, what was asked, and what came back.
  Stated explicitly in the work order.
* **R5 — Containment.** Consulting B never modifies B.
* **R6 — Cost.** No agent burns tokens sitting idle waiting to be asked something.

R4 deserves emphasis: the user asked for traceability *and* for the exchange to bypass work
orders. Those pull against each other, because in this OS the work order **is** the audit
trail — every timeline, message and assumption hangs off a WO id. Any design that routes the
conversation outside work orders has to build a second place for the record to live. That is
doable (§5) but it is a real cost, not a footnote.

## 4. Options

### Option A — Knowledge-only: make cross-project retrieval real

No new agent, no messaging. Fix the asymmetry in §2, add a cross-project search verb, and
tell the worker in its contract that other projects' learnings are searchable.

Concretely: `jarvis learn search "<term>" --all-projects` (or make PR 27's index carry a
per-project topic roll-call), plus a contract bullet — *"before designing something another
project may already have solved, search the knowledge base across projects."*

* **Pros.** Near-zero cost and near-zero new machinery. Ships in a day. No new failure mode.
  Composes with PR 27 rather than competing with it. Satisfies R1, R3, R5, R6 outright.
* **Cons.** Fails R2 badly. It can only surface what B's workers thought to write down, in
  the words they wrote it in — and "how did B implement coordination" is exactly the kind of
  question whose answer lives in B's source, not in a one-line learning. No dialogue, so no
  follow-up. Fails R4 in the weak sense that a search leaves no record anyone reviews.
* **Verdict.** Necessary, not sufficient. Do it regardless; it is Stage 0.

### Option B — Project expert: an ephemeral, read-only consult **(recommended)**

A new verb:

```
jarvis consult <project> "<question>" [--from <wo-id>] [--learn]
```

It spawns **one headless `claude -p` call** — the same invocation shape Neo already uses via
`claude_cli.run_headless()` — with:

* `cwd` = project B's checkout,
* a read-only permission profile (`Read`/`Grep`/`Glob`/`Bash(git log|git show)` plus the
  `jarvis learn` read verbs; no `Edit`, no `Write`, no push, no `jarvis wo`),
* a system prompt that is B's knowledge index plus a fixed persona: *"You are answering a
  question from another project about how this one works. Answer from this repository's code
  and history. Cite files and commits. You are read-only."*

The answer comes back in the caller's hands in roughly a minute and is recorded (§5). With
`--learn` it is also filed as a knowledge row scoped to B, so the second asker pays nothing.

The asking worker calls it from Bash exactly as it already calls `jarvis wo ask`, and — unlike
`jarvis wo ask` — it does **not** have to end its turn, because the call is synchronous.

* **Pros.** Satisfies R1–R6. Grounded in B's real code (R2), so it answers the motivating
  question rather than a keyword-matched approximation. One bounded call, no idle agent (R6).
  No persistent process to supervise, restart or reason about. Read-only by construction (R5).
  Traceability lands in the OS's own database, which is where prime directive 1 says it belongs.
  Reuses `run_headless`, the knowledge index, and the catalog's project registry — the new
  surface is one CLI verb, one table and one persona.
* **Cons.** The expert is cold: it knows B's repo and knowledge base and nothing about B's
  in-flight work orders (fixable by including B's open-WO titles, at some prompt cost). It
  cannot run B's tests, so "does this actually work" is out of reach. A large repo means a
  costly cold read on every consult, with no caching designed in (see assumption A4). The
  read-only profile has to be genuinely airtight — a consult that edits B is the one failure
  the design must not have. Answer quality is model-dependent, with no verification pass.
* **Verdict.** Recommended as the primary mechanism.

### Option C — Consult work order: A files a question-shaped WO in B

`jarvis wo create <B> --consult --answers <wo-A> "<question>"` creates a real work order in B
with `origin='consult'` whose contract is *answer, do not change code*, finishing with
`jarvis wo answer <id> "<answer>"`, which delivers into A's message queue via the existing
`wo_messages` path.

* **Pros.** Reuses essentially all existing machinery — dispatch, worktree, turns, timeline,
  message delivery, Neo, gates. Almost no new concepts. Traceability is free and total: the
  exchange *is* a work order on both sides, so `jarvis wo show` already tells the whole story.
  The B worker can read, run tests, bisect, and ask Neo — it is a full agent, so R2 is
  satisfied more deeply than in Option B. PR 57 has already built the "agent files a WO"
  guards this would inherit.
* **Cons.** Heavy: a whole worker plus a git worktree per question, minutes of latency (fails
  R3 for anything conversational), and it consumes one of B's `max_concurrent` slots against
  work that ships nothing. A changes-nothing work order pollutes B's ledger and, unless
  carved out the way `waiting_pr_merge` was, lands in the user's review queue — the exact
  attention cost the OS exists to avoid. A must park in a new blocked state, which introduces
  **deadlock and cycles**: A waits on B waits on A, or a chain that never settles. Needs a
  depth cap and a cycle check that do not exist today. And it is precisely what the work order
  asked to avoid ("without the need of work_orders between them").
* **Verdict.** Not the default. Keep as the escalation path for consults that need to execute
  code (Stage 2), where the cost is justified.

### Option D — Literal persistent orchestrators in a Claude team (the proposal as written)

One long-lived Claude session per project, mutually addressable.

* **Pros.** Matches the user's mental model exactly. Conversation is natural: a follow-up
  question costs nothing extra. An orchestrator that lives for weeks accumulates project
  context no ephemeral call can reconstruct. If projects ever need genuine negotiation
  (scheduling, shared interfaces, a migration spanning both) rather than Q&A, this is the
  only option on the list that supports it.
* **Cons**, and they are structural rather than incidental:
  1. **No platform primitive exists.** CLI 2.1.220 has no team and no peer-messaging verb
     (§2). Jarvis would build the bus itself — and a bus Jarvis built is Option B or C with a
     persistent process bolted on.
  2. **It reverses a load-bearing decision.** `ASSUMPTIONS.md` §A.1 made the orchestrator a
     daemon precisely because the role needs no intelligence. This would make the highest-uptime
     component in the OS an LLM.
  3. **Traceability moves out of the OS.** The exchange would live in Claude transcripts, not
     in `os.db` or the project DBs — against prime directive 1 ("the CLI is the OS") and
     against the standing rule that the record must stand alone because nobody reads worker
     transcripts. Recovering R4 means mirroring every message back into the OS anyway.
  4. **Idle cost.** A session kept alive to be asked questions either burns tokens or, more
     likely, decays: the headless-turn design already establishes that every turn re-sends the
     full briefing and a resumed session re-derives its configuration from argv. Persistence
     buys less than it appears to.
  5. **No termination condition.** Two agents that can message each other freely have no state
     machine telling them when the exchange is over. Work orders settle; conversations do not.
  6. **A new failure class.** N long-lived agents to supervise, restart, and reconcile after a
     daemon restart — the OS currently has zero long-lived agents and its invariant library
     assumes that.
  7. **Context staleness.** A session started last week holds a view of B that has drifted from
     B's `HEAD`, and nothing tells it so.
* **Verdict.** Reject as the mechanism; keep the intent. Revisit only if consults turn out to
  need multi-turn negotiation rather than question-and-answer (§8).

### Option E — Route it through Neo

Neo already spans every project and already is the worker's first responder, so the worker
would ask in a way it already knows: `jarvis wo ask` and the answer comes back citing B.

* **Pros.** Zero new surface for the worker. Neo already holds cross-project learnings.
* **Cons.** Neo is a decision-maker, not a code reader: no repo access, so R2 is unreachable.
  Its persona is tuned to produce verdicts, and its economics depend on a **byte-stable prompt
  prefix** shared across consecutive calls — injecting B's knowledge index per question breaks
  exactly that. Conflating "decide this for me" with "research another project" degrades both
  roles.
* **Verdict.** Not as the answerer. Valuable as a **router**: when a worker asks Neo a question
  whose answer lives in another project, Neo replies "consult project B" and names the command.
  That closes the discovery gap (R1) for questions the worker did not know to ask.

### Summary

| | A: knowledge | B: consult call | C: consult WO | D: team | E: Neo |
|---|---|---|---|---|---|
| R1 discovery | partial | yes | yes | yes | yes (router) |
| R2 depth (reads B's code) | no | yes | yes, and can execute | yes | no |
| R3 latency | instant | ~1 min | minutes | seconds | ~1 min |
| R4 traceability | weak | designed (§5) | free | must be rebuilt | partial |
| R5 containment | n/a | by permission profile | by contract only | by contract only | n/a |
| R6 idle cost | none | none | none | **continuous** | none |
| New machinery | tiny | one verb, one table | new WO origin + blocked state + cycle guard | orchestrator lifecycle, bus, supervision | prompt changes |

## 5. Traceability model

The user asked for this explicitly, so it is designed rather than inferred. A consult is
cross-project by nature, so its record belongs in the **central** database (`$JARVIS_HOME/os.db`),
alongside the other things that must be unified.

**New table `consults`** — `id`, `ts`, `from_project`, `from_wo` (nullable: Jarvis and the user
can consult by hand), `to_project`, `question`, `answer`, `mechanism` (`call` | `wo`), `status`
(`asked` | `answered` | `failed`), `answered_at`, `wo_id` (set only when escalated to Option C).

**Surfaces**, one derivation feeding all of them, per the post-conditions design:

* On the **asking** side — `consult_asked` / `consult_answered` events on A's work order, so
  the exchange appears in `jarvis wo show <A>` and in the timeline the user already reads.
* On the **answering** side — B has no work order to hang it from, so B's project page in the
  dashboard gets a "questions other projects asked about this one" panel, and
  `jarvis consult list --project B` prints the same thing.
* `--learn` files the answer as a knowledge row scoped to B and tagged `consult`, which is how
  the second asker gets it for free.
* **No inbox item on a successful consult**, deliberately, and for the same reason a dismissed
  gate raises none: routine machinery working correctly must not spend the user's attention.
  A *failed* consult does raise attention, because a worker silently missing an answer it was
  told to expect is how a wrong design gets built confidently.

## 6. Recommendation — staged

**Stage 0 — make cross-project knowledge honest (Option A).** Either widen `relevant_knowledge`
or fix the prompt header that claims other projects' learnings are present when they are not,
and add the cross-project search bullet to the worker contract. Small, independent, and it
makes the value of the later stages measurable. Sequence after PR 27, which rewrites this exact
code path.

**Stage 1 — `jarvis consult` (Option B) plus the `consults` table and surfaces (§5).** The
primary deliverable.

**Stage 2 — `--escalate` to a consult work order (Option C)** for the questions that need to
run B's code, inheriting PR 57's guards plus a depth cap and a cycle check.

**Stage 3 — standing project experts (Option D), only if measured.** The trigger is evidence,
not taste: if consult volume against one project is high enough that cold reads dominate cost,
or if exchanges routinely need more than two turns, revisit. Until then the numbers say a
persistent agent buys nothing that an ephemeral call does not.

Each stage is independently useful and independently abandonable.

## 7. Assumptions

Every one of these is a decision made while writing this, and any of them may be wrong.

1. **"Orchestrator" means the per-project agent role**, which today is `jarvisd` and is not an
   agent. The design therefore does not create persistent per-project agents. If the user
   actually wants literal always-on project agents as a goal in itself, Option D is the answer
   and this recommendation is wrong.
2. **The user wants the capability, not the implementation.** "A Claude team" is read as a
   proposed means to "A can learn from B", and the means is treated as negotiable.
3. **Read-only is sufficient.** A consult never modifies B. Anything that should change B is a
   work order in B, created the normal way.
4. **Consults are rare** — single digits per day fleet-wide. No caching, deduplication or
   rate-limiting layer is designed. If they become chatty, per-project answer caching is the
   first thing to add.
5. **A ~1–2 minute latency budget is acceptable**, the same envelope as `jarvis wo ask` → Neo.
6. **The asker is usually a worker**, but `jarvis consult` is a normal CLI verb the user and
   Jarvis can run by hand. It is not worker-only.
7. **A consult is not a privileged action.** It publishes nothing, spends nothing beyond
   tokens, and touches no production system, so it does not gate. Note this assumption is load-
   bearing for R3: gating a consult would make it as slow as a work order.
8. **All projects are local checkouts on one machine**, registered in the catalog. No
   cross-machine or remote consult.
9. **Trust is symmetric** — any registered project may consult any other. No ACLs in v1. All
   projects are the user's own.
10. **Answers stay local.** A consult answer may quote B's source; it is stored in local SQLite
    and never published. It does, however, cross a project boundary — see the secrets risk in §8.
11. **Design only.** Every number here (limits, timeouts, table columns) is a proposal to be
    settled at implementation time, not a specification.
12. **PR 27 and PR 57 land first.** Stage 0 conflicts textually with PR 27 and Stage 2 depends
    on PR 57's guards. If either is abandoned, the corresponding stage needs redesign.

## 8. Risks and open questions

* **Secrets crossing a project boundary.** The sharpest one. A consult answer quoting B's source
  lands in A's worker transcript, A's timeline and the central DB — and A's worker may be running
  under a different permission profile than B's. Options: strip nothing and accept it (all
  projects are the user's), run the answer through a secret-scanner, or restrict the consult
  persona to describing approaches rather than pasting code. **Wants a decision before Stage 1.**
* **Cycles and deadlock**, if Stage 2 ships: A consults B consults A. Needs a depth cap and a
  cycle check; PR 57's "a `neo`-origin WO never dispatches another" guard is the same shape.
* **Cost blowup on a large repo.** A cold consult against a big checkout can read a lot before
  answering. Needs a turn/token cap and a stated failure mode when it is hit.
* **Answer quality without execution.** Option B cannot verify its own claims. Accepted for v1;
  Stage 2 is the escape hatch.
* **Auto-filing answers as knowledge** (`--learn`) risks polluting the base with
  near-duplicates — and `add_knowledge` is append-only with no supersede, so a wrong entry is
  permanent. Suggest `--learn` stays opt-in until retraction exists.
* **Does the user want to approve consults?** Assumption 7 says no. If they do, the latency
  budget (R3) changes and Option C becomes comparatively more attractive.
* **Discovery is still the weak link.** Nothing makes A's worker realise B is worth asking unless
  the user says so in the work order or Neo routes it (Option E). Stage 0 helps; a real fix may
  need per-project capability summaries in the index.

## 9. Testing sketch

Consistent with `mem:testing` — deterministic tests against the fake `claude` executable, with
model-dependent behaviour behind the opt-in LLM evals.

* Unit: the `consults` table round-trip; `jarvis consult` against an unregistered project errors
  the way `find_work_order` does; a failed consult raises attention and a successful one does not.
* Permission profile: assert the consult invocation carries no `Edit`/`Write` grant — a mutation
  test that removes the restriction must fail a test, since "read-only" is the containment claim.
* Traceability: `consult_asked`/`consult_answered` appear in A's timeline; B's project page lists
  the consult.
* Cycle guard (Stage 2 only): A→B→A is refused at the depth cap.
* LLM eval (opt-in): given a real second project, does the expert persona answer from that
  repo's code and cite files, rather than answering generically?

## 10. Out of scope

* Remote or cross-machine consults (assumption 8).
* Per-project ACLs on who may consult whom (assumption 9).
* Multi-turn consult conversations — v1 is one question, one answer. Follow-ups are new consults.
* Any change to how work orders are created, dispatched or settled.
* Persistent per-project agents (Option D) — deferred to Stage 3 behind a measured trigger.
