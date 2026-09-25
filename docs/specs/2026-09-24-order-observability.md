# Order observability: see what an order is doing, right now and afterwards

Feature order `fo-ff8570fa`. Spec written by the planner of `wo-83e4183c`, 2026-09-24.

A user watching a Jarvis order today can see what it *cost* and what *state* it is in.
They cannot see what it is *doing*. This feature closes that: one live view of the turn in
flight, one full anatomy of every turn that finished, one ledger of what Jarvis put in the
context window, one report that says why an order is not moving — on the CLI first, and on
a dashboard page that renders the same four payloads.

Sections 3–7 are the work. Sections 1, 2 and 8 are the background every worker needs and
belong to nobody.

---

## 1. The evidence: what forty-seven closed bugs say the user could not see

This is not motivation prose. It is the input the feature order asked for — the last ~50
completed orders read back — and it decides which of the four new surfaces gets built at
all, and what each one leads with. Source: every closed issue on the tracker excluding
`validation follow-up` rows, 47 of them, plus the merge log of the last 60 orders.

**Class 1 — an order parked for ever and nothing said why. Eight issues, the largest
class by a wide margin.** #705 (a repaired pull request stranded behind two false
blockers, no self-heal), #493 (nothing re-opens a round when the judged commit is no
longer the head), #480 (a feature manager never told its children landed), #264 (a manager
reporting itself IDLE parked as "Waiting on you", for ever), #259 (a usage-limit retry
that came due and never fired), #197 (a worker parked on a held gate flagged as waiting
for the user), #711 (assumptions flag a running order "Needs you" where Neo auto-reviews),
#100 (`resume-auto` a no-op fleet-wide). Every one of these is the same user experience:
the order sits, the surface shows a status, and the status does not name the thing that
has to happen next. **This class is why §6 exists, and why it is not optional.**

**Class 2 — the bill was wrong, or right and useless. Six issues.** #692 (a
budget_exhausted order's WORKER spend printed against its WHOLE cap), #471 ($0.00 while a
turn burns, and a turn's cost lost for ever when reaped without an envelope), #470 (bill
overstated 3–6x because `modelUsage` is session-cumulative), #227 (a turn that made no API
call reported as 100% generating), #103 (`claude` subprocesses missed), #216 (a health
sweep that failed 2215/2215 times and spent $101.78 for zero findings). The arithmetic
bugs are fixed. What is not fixed is that a reader who suspects the number has nothing
underneath it to check against — no per-turn call list, no tool profile on the same page,
no way to see *which* call produced *which* token class. §4 and §7 answer that.

**Class 3 — state inconsistency with no self-heal. Five issues.** #271, #263, #253, #232,
#240. Diagnosing these needed the OS's own record of what it decided and when, which the
timeline holds but which no single surface puts beside the worker's activity.

**Class 4 — the OS's own model calls failing silently. Two issues, both severe.** #216
(the health sweep above) and #199 (validation escalation reaching neither Neo nor any
notification sink). `agent_usage` now records every OS-side `claude -p` call against the
order that caused it — but only the *cost* of it is surfaced, on the bill's `jarvis`
column. A call that failed, retried and gave up is invisible. §6 surfaces it.

**Class 5 — gate recogniser false positives. Five issues** (#233, #203, #194, #185, #104).
Already served by `jarvis gate explain`. **This feature adds nothing here**; it is listed
so a worker does not go looking for a gap that was closed.

The ranking above is the design input. Class 1 outnumbers everything, so the diagnosis
report (§6) is a first-class child and not a footnote on the debug page. Class 2 says the
new surfaces must *show the underlying rows*, not a prettier total.

## 2. What already exists, and the standing rules this feature must not break

Read this before proposing anything. Most of what the feature order asks for is already
computed and merely has no surface.

**Already computed, reachable only through `jarvis inspect`, with no dashboard page at
all.** `src/jarvis/inspection.py` walks the transcript (`read_transcript`, `read_session`)
and produces an `Anatomy`: per-turn wall clock split across
`PARTS = ("generating", "blocked", "tools", "idle", "unaccounted")`; every cache write over
a per-project floor classified by cause as `cold-start` / `ttl-expiry` / `prefix-miss` /
`compaction`, each with a prose note in `WRITE_CAUSE_NOTES`; context size per call
(`Call.context`) and peak per turn (`Turn.context_peak`); the 5m-versus-1h TTL split
(`Anatomy.cache_ttl`) and `rewrite_excess`; every tool call as a `ToolSpan`, including ones
that never returned (`unfinished`); blocking joins on subagents; and the OS's own holds
(`holds.held`) clipped into each turn. `ops.inspect_report` is the shipped entry point and
`cli._print_anatomy` the only renderer.

So: **the prefix-break moment, its cause and its price are already done.** A worker who
sets out to build cache-write classification has misread this spec.

**Live reading already works.** `inspection.live_alarms` reads a *running* session's
transcript on every reconcile tick from `Daemon.check_burning_turns`. The capability
exists; there is no surface a user can point at a running order.

**Already computed elsewhere.** `ops.waiting_on(store, wo)` returns
`{"what", "detail", "stalled"}` naming the exact command that clears the block, for
assumptions, escalated gates, gates with Neo, held-and-unargued gates, Neo questions and
more. `invariants.true_blockers` answers the narrower "does this need the USER".
`invariants.parked_reason`, `invariants.pause_note`, `invariants.fleet_hold_note` and
`invariants.status_label` each answer a slice. `holds.held` returns the hold episodes with
causes and durations. `agent_usage` records every OS-side call against the order.
**None of these are wrong and none are to be rewritten.** §6 is an aggregation.

**Derivable today but thrown away.** `inspection._detail_of` reduces a tool call's whole
`input` dict to one string — first hit of `description|command|task_id|file_path|pattern|
skill`, truncated to `cfg.quote_chars`. The full input is in the row and is discarded.
And `inspection._subagent_transcripts` returns the subagent JSONL paths, but nothing calls
it for turns or spans: `read_session` reads subagent metadata for *labels* only
(`_subagent_labels`), so a subagent's own tool calls, tokens and cache writes are invisible
in `jarvis inspect`. Both are parsing work over data already on disk. §4.

**Genuinely absent, and the only new persistence this feature adds.** Claude Code does not
write the rendered system prompt anywhere Jarvis can read. `hooks.prefix_fingerprint` says
so in its own docstring and fingerprints the *ingredient list* for exactly that reason
(knowledge entry `kn-0cb81cec`: "a hook cannot hash the rendered system prompt either, so a
prefix fingerprint is a list of ingredients"). Context composition therefore cannot be
recovered after the fact and must be measured at dispatch. §5.

**Three measured facts, verified for this spec on Claude Code 2.1.282. Do not re-derive
them.**

1. A `tool_use` row is written to the transcript when the assistant message completes,
   which is *before* the tool runs. Measured on a live worker transcript: eight
   consecutive `Bash` calls, ask stamp `15:11:56.665` against result stamp `15:11:59.771`,
   and so on. **A tool that has been running ten minutes is therefore visible right now**
   as a `tool_use` with no matching `tool_result` — exactly `ToolSpan.finished == False`.
   The live view in §3 rests on this and on nothing else.
2. The transcript carries the full `tool_use` `input` dict verbatim. Parameters are a
   parsing question, never a capture question.
3. The transcript carries no system prompt, no skills list and no agent definitions. Row
   types present are `assistant`, `user`, `attachment`, `system`, `cost-state`,
   `last-prompt`, `agent-name`, `worktree-state`, `mode`, `pr-link`, `queue-operation`,
   `custom-title`, `atis-latch`. There is no row that says what was in the window.

**The standing rule of `inspection.py`, from its own module docstring, and it governs
every child of this feature:** *"Everything here is arithmetic over files Claude Code
already wrote… there is nothing to store and nothing to reconcile — and a transcript that
has expired is reported as absent rather than guessed at."* Every surface added by this
feature reports `found: false` when the transcript is gone. **Absent is not zero.** A
report that prints "0 seconds" for an expired transcript is making a false claim, and that
exact confusion is issue #227.

**The layering.** Imports run strictly downward: leaves → stores → adapters →
`dispatch`/`ops` → `daemon`/`cli`/`ui`. Business logic goes in `ops.py` and is consumed
identically by CLI and UI; a renderer that computes anything is a renderer the other
surface will disagree with. That is how a listing and a header once came to disagree about
the same work order (PR 65), and it is why every section below specifies an `ops` function
returning a dict, with both renderers consuming that dict verbatim.

**Everything this feature adds is metered, and everything it writes is gated.** Every
surface below is metered through §10, every WRITE any of them makes is gated by §10's
configuration, and §10 lands before any of them. The detail is there; this spec says each
thing once.

---

## 3. The live turn snapshot: `jarvis watch` and `ops.live_report`

**The question this answers:** "this turn has been going two minutes — what is it doing
*right now*?"

**New module `src/jarvis/live.py`.** Deliberately a new file and not an addition to
`inspection.py`: §4 rewrites the walk inside `read_transcript`, and two children editing
that function conflict for no gain.

**The incremental reader.** `usage.rows` re-parses the whole transcript on every call and
`usage.index_sessions` walks every transcript on disk. Both are correct at reconcile
cadence and wrong for a two-second refresh. `live.py` resolves the session's transcript
path **once** per watch, then reads forward from a byte cursor. The resume-cursor pattern
to copy is `uilog.read_errors`: byte offset plus inode plus a hash of the file's first
line, so a rotated or replaced file is detected rather than read as a continuation.

**The `Live` dataclass**, and `ops.live_report(target, project=None) -> dict` as the single
shipped entry point. Both renderers consume exactly this and neither reshapes it:

```
{"wo_id", "project", "session_id", "found": bool,
 "state": "working" | "generating" | "idle" | "settled" | "no-transcript",
 "turn": {"seq", "started", "elapsed", "triggers": [...]} | null,
 "now": {"tool", "detail", "params", "started", "elapsed"} | null,
 "recent": [ {span…}, … ],          # last N finished spans, newest first
 "tokens": {"input", "output", "cache_write", "cache_read", "context"},
 "last_write": {"ts", "written", "read", "cause", "note"} | null,
 "stale_seconds": float,            # since the last row of any kind
 "subagents": {task_id: label},
 "holds": [ {hold…} ]}
```

**The blind spot, and the words for it.** A row lands when a *message* completes. A model
that has been generating for ten minutes with nothing emitted yet is invisible. The
snapshot must report `state: "generating"` with `stale_seconds` and say, in those words,
that nothing has been written since a given time. **It must never invent activity, and it
must never report a stale reading as a current one.** This is the same discipline as
`kn-2bba079c`: a remainder is not a measurement, and every layer above one will treat it
as one.

**`jarvis watch <wo-id>`** repaints the snapshot on an interval (`--interval`, default 2s,
`--once` for a single frame, `--json` for the payload). It is visually oriented, per the
feature order: the in-flight tool and its elapsed time is the largest thing on the screen,
then the turn clock, then the token counters, then the recent spans. It works on a settled
order too, reporting `state: "settled"` and the last frame, so a user who runs it a minute
late gets an answer rather than an error.

**What it must not do.** No new table. No hook. No persistence of any kind. No polling of
subagent transcripts (see §8). It reads one file.

**What is provable and what is a hand check.** `--once --json` is fully testable against a
fabricated transcript. The repaint loop, the `--interval` behaviour and the visual
hierarchy are not, and this repository has no terminal-capture harness. Prove the payload
in the suite; verify the repaint by hand against a live order and put the screenshot in the
pull request. Do not invent a test that asserts nothing in order to have one.

## 4. Turn anatomy in full: tool parameters and subagent sessions

**The question this answers:** "which tools ran, with what arguments, and what did the
subagents do?"

Two additive changes to `src/jarvis/inspection.py`, and one rendering pass.

**(a) Tool parameters.** `ToolSpan` keeps the full `input` dict alongside today's one-line
`detail`, which stays exactly as it is so every existing renderer and every existing test
keeps working. The site is `_detail_of`. Three constraints, all load-bearing:

- **Bounded.** A tool input can be an entire file. Cap per-value and per-span, cap the
  total per turn, and state the cap in the payload the way `Anatomy` already states its
  `write_floor` and `join_floor` — a report that truncates without saying so is not
  reproducible.
- **Redacted.** Tool inputs carry file contents, tokens, and anything a worker typed into
  a `Bash` command. Redaction is a named, tested function, not an inline regex, and it
  must be applied before the value reaches any payload. The reference for how carefully
  this is taken in this codebase is `kn-32fa2a7d` (a reviewer prompt must bound every
  field it did not write) and `kn-deef42ea` (if the text may not travel, the row must not
  either).
- **Additive.** `Anatomy.as_dict` grows keys and changes none. Every existing renderer and
  every existing assertion against the committed real session keeps working untouched.

**(b) Subagent anatomy.** `read_session` walks `_subagent_transcripts` and returns each
subagent as a nested anatomy — its own turns, spans, calls and cache writes — attached to
the parent turn it ran in. **A subagent is a PARTITION of its parent turn, drawn out of
it, never added to it.** That is the bill's existing rule (`kn-7a2180ba`) and breaking it
here would make `jarvis inspect` and `jarvis cost` disagree about the same order. State
the nesting depth actually supported: `_subagent_labels` globs one directory level, so a
subagent spawned *by* a subagent may not be reachable. Check it, and say in the payload
which depth was read rather than implying completeness.

**There is no real subagent transcript in this repository, and you must not create one.**
`tests/data/transcripts/-wo-5a6b2d6d/…/subagents/` holds `.meta.json` files only; the
transcripts themselves were dropped by `scripts/redact_transcript.py`. Eleven existing
tests pin exact totals against that committed session, so adding a `.jsonl` there would
move numbers across the suite. Synthetic transcripts are therefore the only safe proof for
this half — which means nested subagent anatomy is proven against fabricated data. That is
honest and it is weaker than the rest of the file; do not write it up as anything stronger.

**(c) The visual pass on `jarvis inspect`.** `cli._print_anatomy` is dense text. The
partition per turn is already in `turn["share"]`; render it as bars. Add the tool profile
and the new parameter detail behind a flag so the default output does not grow unbounded.
`cli.PART_LABELS` is keyed by `inspection.PARTS` and `tests/test_inspection.py` pins the
two equal — a bucket that exists in one renderer and not the other is a table that no
longer sums to 100.

## 5. The context ledger: what Jarvis put in the window, and the delta

**The question this answers:** "how much of this context is system prompt, how much is
skills, how much is the knowledge block — and what changed since the last turn?"

This is the one section that adds capture and schema, because §2 establishes that nothing
on disk answers it.

**New module `src/jarvis/context.py`.** At dispatch, measure every ingredient Jarvis
itself supplies and can therefore account for: the appended system prompt and common
briefing (`dispatch.build_worker_prompt`, `_common_briefing`), the knowledge index block
(`dispatch.render_knowledge_block`), the worker settings file (`_write_worker_settings`),
the project `CLAUDE.md` and the memory files (`hooks.memory_files`), the agent persona, the
skills and `--add-dir` contents, and the MCP server set (`wiring.py`). Each ingredient is
recorded with its byte count and an estimated token count, and with `measured: true`.

**The residual is inferred and must be labelled as such.** Claude Code's own base system
prompt and the tool schemas are not Jarvis's to measure. Attribute them from the
`cold-start` cache write that `inspection.classify_writes` already identifies — observed
prefix minus the sum of measured ingredients — and carry them as one row with
`measured: false`. **Never present a byte-exact figure for the rendered system prompt.**
It does not exist and claiming it would be the defect `kn-0cb81cec` already warns about.

**Persistence.** One payload per turn. Prefer a JSON column on the existing `wo_turns`
table in the per-project DB (`<project>/.jarvis/jarvis.db`) over a new table: it is one
blob per turn with the same lifetime and the same owner. Follow `ProjectStore._migrate` /
`ADDED_COLUMNS`. `kn-c712a5d6` is the rule for testing it: a new column is untested until
a test writes it *and* a test reads a row that predates it.

**The delta, and the join that makes this worth having.** `ops.context_report(wo_id,
project) -> {"turns": [{"seq", "ingredients": [...], "delta": {...}, "prefix_break":
{...} | null}]}`. The delta is this turn's ingredients against the previous turn's. The
`prefix_break` key joins a delta to the `prefix-miss` writes `classify_writes` already
labels, so the report can say **"the prefix broke at turn 5 and the knowledge block grew
4.2k between turn 4 and turn 5"** — one sentence naming a cause, which is the entire point
of the section. Where the two disagree, the write classification is the authority and the
delta is the hypothesis; say which is which. `kn-fafe92b7` is explicit that
`invariants.check_prefix_stable` is the authoritative prefix-stability measurement and
every other signal in this tree is a proxy that defers to it, and `kn-2c41d4cc` is
explicit that a proxy earns its place beside an authority by naming a cause, not by raising
a second alarm.

**What the residual can and cannot be proven to be.** A test fabricates the transcript it
then reads the `cold-start` write out of, so it asserts the arithmetic and the labelling
against a number the test itself wrote. That proves the subtraction and the `measured:
false` flag; it does not prove the attribution is true of a real session, and there is no
way to prove that in CI. Say exactly this in the pull request rather than a stronger claim.

**Forward-only, and the surface must say so.** The ingredients of orders that already ran
were never recorded and cannot be recovered. An order that predates this landing renders
"not recorded for this order" — not an empty table, which a reader will report as a bug.

**CLI surface:** `jarvis wo context <wo-id>` (`--json`, `--turn N`).

## 6. Why is this order not moving: the diagnosis report

**The question this answers, and it is the one §1 says the user asks most:** "this order
has not moved in three hours. What is it waiting for, and what do I type?"

The parts exist and are scattered across three surfaces that each show a slice. This
section is the aggregation and one new ingredient. **Rewrite none of the underlying
functions**; a diagnosis that disagrees with the status label is worse than no diagnosis.

`ops.diagnose(wo_id, project) -> dict` composes:

1. **The blocker**, from `ops.waiting_on` — which already names the exact command that
   clears each case — cross-checked against `invariants.true_blockers` (does this need the
   *user*), `invariants.parked_reason`, `invariants.pause_note`, `invariants.fleet_hold_note`
   and `invariants.status_label`. Where two disagree, **report both and say they disagree**.
   A silent pick is how #197 and #711 reached the user as the wrong sentence.
2. **The clock, read from the OS's own record and from nothing else**: the last state
   transition and how long ago, the last worker turn (`wo_turns`) and how long ago, the
   last timeline event, and whether a turn is currently open. **It does not call §3.**
   That is deliberate and it is a seam, not an omission: this section answers "what does
   the OS's record say about this order" and §3 answers "what does the transcript say is
   happening in the process". Coupling them would make this section unbuildable until §3
   lands, for one number. Where a live reading is what the user wants, say so and name the
   command: `jarvis watch <wo-id>`. The debug page (§7) renders both blocks anyway.
3. **The holds**, from `holds.held`: every episode with its cause and duration, the open
   one marked. `Anatomy.unexplained` is the honest residual — idle the record does *not*
   account for — and a large one is a different defect from anything this report can name.
   Print it as the residual it is, never as a cause.
4. **The OS's own calls for this order** — the new ingredient, and the answer to class 4 of
   §1. `agent_usage` records every OS-side `claude -p` call against the order that caused
   it: Neo answers, panel seats, digests, worker subprocesses. Surface each with its
   outcome and **especially its failures**: a call that errored, retried and gave up is
   today visible only as a cost row. Pinned rule `kn-40db1828` applies with full force —
   **a model that was never reached has made no judgement, and the OS must never synthesise
   one**; this report is how a user sees that a call was unreachable rather than decided.
5. **What to do**, as literal commands the user can paste: the clearing command from (1),
   plus `jarvis wo unblock`, `jarvis wo resume-auto`, `jarvis validation force`,
   `jarvis wo ack`, `jarvis gate …` where each genuinely applies. **Only offer a command
   the OS would actually accept right now** — `jarvis validation force` refuses on a
   running order, `jarvis wo ack` refuses on pending assumptions. Offering a command that
   will be refused teaches the user to distrust the surface.

**CLI surface:** `jarvis wo why <wo-id>` (`--json`). Works on a settled order too,
reporting how it settled.

**The boundary.** This report explains and offers; it never acts. It files nothing,
unblocks nothing and sends nothing. The acting path is `remedies.py`, which is a closed
registry behind a Neo approval and a grant, and is out of this feature entirely (§8).

## 7. The debugging view: the dashboard page and the JSON behind it

**The question this answers:** "show me all of the above on one page."

`GET /wo/{name}/{wo_id}/debug` — a new route, deliberately not the existing `?debug=1`
query parameter on `/wo/{name}/{wo_id}`, which means something else entirely (show
debug-level timeline events, `timeline.count_debug`). Do not overload it.

Plus `GET /api/wo/{name}/{wo_id}/live` returning `ops.live_report` as JSON, polled by the
page. **Poll. No SSE, no websockets, no JS build step** — a meta refresh or a dozen lines
of inline JS, consistent with the rest of `ui/app.py`.

The page renders, in this order, because §1 ranks them in this order:

1. **The diagnosis** (§6) at the top, always. Class 1 is the largest bug class and the
   answer to "why is nothing happening" must not be below the fold.
2. **The live snapshot** (§3) when the order is running; the last frame when it is not.
3. **The full anatomy** (§4) — per-turn partition bars, the tool profile, the tool calls
   with their parameters, the subagents nested under the turn they ran in, the classified
   cache writes with their prose causes. **`ops.inspect_report` has no dashboard surface at
   all today**; this is it.
4. **The context ledger** (§5) when recorded, with the forward-only note when not.

**Cross-links both ways with the bill.** The feature order's complaint is that
`/cost/{name}/{order_id}` shows only money. It keeps doing exactly that — this feature does
not change the bill — but the two pages link to each other, and `jarvis cost` gains one
line naming `jarvis inspect`, `jarvis watch` and `jarvis wo why` as where the detail lives.

**Every payload comes from `ops` verbatim.** The route computes nothing. If a number is
wrong the fix is in `ops`, once, and both surfaces get it.

**Proof goes in the default suite, not the browser job.** Route tests use fastapi's
`TestClient`, the way `tests/test_ui_cost.py` does. `tests_browser/` is Playwright, runs as
a separate CI job needing `playwright install chromium`, and is not in the default
`testpaths` — a test there is optional extra, never the evidence. The poll actually
refreshing is a hand check with a screenshot, like §3's repaint.

**Degradation is a feature, not an error page.** Each of the four blocks renders
independently: an expired transcript, an order that predates the context ledger, or a
settled order with no live session each render their own honest note. One missing block
must not 500 the page — `uilog` turns a dashboard 500 into an inbox item and
`INV-UI-HEALTHY` into a fleet alarm.

---

## 8. Out of scope, and why

Filed to the backlog where a backlog item is the right home. Listed here so nobody
re-proposes them mid-feature.

**OTEL is not out of scope here, and it is not rejected.** Whether Claude Code's
OpenTelemetry export earns a place in this tree is UNDECIDED, and §9 owns that decision.

**Hook-recorded tool spans, and any live-state table. Rejected on the merits, not deferred
for size.** `PostToolUse` is already matched in `assets/settings.base.json`, so a hook would
add no subprocess cost — but it would add a write path, a table, a migration and a
reconciliation problem, to record facts Claude Code already writes to a file Jarvis does
not have to own. §2's standing rule stands: nothing in this area is persisted except the
one thing Jarvis alone witnesses, which is §5.

**New alarm kinds or thresholds.** `inspection.ALARM_KINDS` is coupled to
`probes.RESERVED_IDS` — a probe id may not shadow an alarm kind — so adding kinds drags the
supervisor into this feature. The existing alarms keep firing unchanged.

**Any acting or repair path.** This feature explains; `remedies.py` acts, behind a Neo
approval and a one-use grant. The feature order asked for "tools to fix those identified
issues"; the fix affordance delivered here is §6's literal, pre-validated commands, which
is the largest honest step. Automating them is a separate feature.

**Retroactive context composition.** Impossible: the ingredients of past orders were never
recorded. §5 is forward-only and says so on the surface.

**A byte-exact rendered system prompt.** Claude Code does not write one anywhere Jarvis can
read (§2). Ingredient measurement plus a labelled inferred residual is the ceiling.

**Live subagent tailing.** Settled subagent anatomy only (§4). Tailing N subagent
transcripts per refresh multiplies the read cost for a rare case.

**Fleet-wide "what is burning now"** — `jarvis watch` with no argument, and the same strip
on the dashboard home. Genuinely useful, purely additive on top of §3, and droppable.
Backlogged.

**Changing the bill's arithmetic.** `bill.py` is correct after six fixed issues and is
sealed on settlement (`kn-3629fa87`). Its existing lines, its arithmetic and that seal are
untouched. The one change this feature makes to the bill is the observability class §10
adds, and it arrives the way every other kind already does — as `agent_usage` rows read by
the reporting `bill.py` already has, not as new maths inside `bill.py`.

---

## 9. OTEL: the decision, and the measurement that settles it

**This section produces a decision, not a feature.** The feature order asked for Claude
Code's OpenTelemetry export to be assessed *first*, and nothing in this tree mentions OTEL
today. The deliverable is a verdict — adopt, decline, or adopt in part — carried by
evidence and reached here, not a confirmation of a decision taken anywhere else in this
spec. It runs in parallel with §§3–7 and gates none of them. **A decline is as full a
success as an adoption.** A measurement that lands where the planner expected is worth
exactly as much as one that does not, and a worker who feels pressure to produce a
recommendation in either direction has misunderstood the job.

**The prior this measurement tests, and it is reasoned, not measured.** The planner
expected a decline and wrote down why. What follows is the hypothesis the six questions
below are pointed at — each reason is confirmed or contradicted by what actually arrives —
and it is explicitly **not a verdict**. (1) The export is metrics and log events — session,
token and cost counters, tool-decision events, aggregated on a flush interval — and carries
no tool parameters, no cache-write cause, no context composition and no per-call
`modelUsage` breakdown; every one of those is already parsed with strictly more fidelity by
`usage.py` and `inspection.py`, including the 1h-versus-5m cache-write split that a counter
cannot express. (2) A collector is a new long-lived process, a new port and a new failure
mode against a core that is deliberately stdlib-only, with many concurrent headless workers
each exporting. (3) Metrics flush on an interval, so a turn that *dies* — the case most
worth debugging — may never flush; the transcript is on disk throughout. (4) The cost of
keeping the door open is one line: the env seam is `claude_cli.spawn_turn`'s
`env = {**os.environ, **cache_env()}`. Adding OTEL later is a dict, not an architecture.
**Nobody ran `claude` with `CLAUDE_CODE_ENABLE_TELEMETRY=1` and enumerated what arrives**,
which is exactly why those four reasons settle nothing on their own.

**Timebox: one session.** If the measurement is not in hand by then, report what was
measured and what was not. Do not extend into building an integration.

**The measurement.** Run a real headless `claude -p` turn with
`CLAUDE_CODE_ENABLE_TELEMETRY=1` and an OTLP endpoint pointed at something that records
everything and interprets nothing — a throwaway local listener is fine, and is preferable
to a full collector because the question is *what arrives*, not *what a collector does with
it*. The env seam in this codebase is `claude_cli.spawn_turn`'s
`env = {**os.environ, **cache_env()}` plus `cache_env`; for a spike, setting the variables
in the shell around a scratch invocation is entirely sufficient and touches no Jarvis code.
`scripts/spike_peer_message.py` is the precedent for how a spike is written and reported in
this repository — read it before you start.

**The six questions to answer, and answer each with what arrived or with "nothing".**

1. What metric and log-event names arrive at all? Enumerate them.
2. Does any carry a **tool name with its parameters**? (§4 needs this; the transcript has
   it.)
3. Does any carry a **cache-write cause**, or the 5m-versus-1h TTL split, or enough
   per-call `modelUsage` detail to derive either? (§2, `inspection.classify_writes`.)
4. Does any carry **context composition** — the share of the window taken by the system
   prompt, skills or agents? (§5 exists only because the transcript does not.)
5. What is the **flush interval**, and does anything arrive from a turn that is **killed
   mid-flight**? This is the third reason above and the one most likely to be wrong.
6. What does it cost to run: process count, port, failure modes with many concurrent
   headless workers.

**The deliverable** is a findings document under `docs/specs/`, one knowledge-base entry
via `jarvis learn add --project jarvis_os --topic observability`, and a pull request
containing both. **Write no production code.** If the finding is that OTEL adds something
the transcript does not, the deliverable is still the finding — file the integration as a
separate feature order and say so; do not start building it in this session.

**Write the decision into §8 in the same pull request.** On a decline, §8 gains a new
entry — declined, and here is the measurement that declined it — carrying the evidence and
striking whichever of the four reasons above the measurement contradicts. On an adopt, or
an adopt in part, §8 gains one line saying OTEL has moved out of this feature into a named
follow-on feature order, with that order's id. §8 is the only prose outside this section
that any child of this feature may edit, and it belongs to this one.

---

## 10. What observability costs, and who turns it on

**This section lands FIRST of every child, even though it is numbered last.** Section order
in this spec is reading order, not build order: §§3–7 each depend on the gate and the meter
defined here, so a reader who takes the numbering for a schedule has it backwards.

**The gate.** A new `ObservabilityConfig` dataclass, added to the fleet-level config
dataclass AND the project-level one, exactly the way `InspectConfig` already appears in
both (`src/jarvis/catalog.py` lines 1009 and 1118). The project object is built on the
fleet object as its base, so one caller reads one field and never consults two objects —
copy that construction and do not invent a second lookup. One field to start: `level`, one
of `off`, `normal`, `full`. A per-order override lives in a new `work_orders` column,
following `budget_usd`'s precedent in `ProjectStore.ADDED_COLUMNS` — nullable, and NULL
means "this order has no answer", which is **not** the same as `off`. Precedence is stated
as a rule and tested as one: the order column, else the project config, else the fleet
config. Default `normal`.

**What the gate actually governs, and what it must not.** Only WRITING. §§3, 4, 6 and 7 are
arithmetic over files Claude Code already wrote — they collect nothing, and gating a
read-only computation would buy the user nothing while costing them the very view they
opened. §5's per-turn ingredient row is the one real collection this feature adds, and it
is the thing `off` switches off. Say it plainly, on the surface and in the config's own
docstring: `off` does not disable `jarvis watch`, `jarvis inspect`, `jarvis wo why` or the
debug page. `full` is the level at which §5 records, and at which the meter below records
its per-report rows.

**The meter.** `src/jarvis/observability.py`, which is also where the precedence resolver
lives. Every observability payload — `ops.live_report`, `ops.inspect_report`,
`ops.context_report`, `ops.diagnose`, and §5's dispatch-time write — is wrapped so each
invocation records one row through the EXISTING `agent_usage` seam (`agent_usage.record` /
`agent_usage.recorder`, `src/jarvis/agent_usage.py`), under new kinds, against the work
order it was run for. Wall clock is recorded; tokens are recorded as they are actually
reported, which for a pure-arithmetic path is zero. **That is the point.** The claim
"debugging is mechanical" becomes a measured zero in the bill rather than an assertion in
this spec, and if any of these paths ever gains a model call, the row stops reading zero on
its own and nobody has to remember to instrument it.

**The bill line.** `agent_usage` rows already reach `jarvis cost` and the /cost page
through `bill.py`'s `_call_items` / `_agent_items`, and `WORKER_SUBPROCESS` is the existing
precedent for reporting a kind as its OWN CLASS rather than folding it into Jarvis's
overhead. Observability is a third class beside the worker's turns and Jarvis's overhead,
for the same reason: money the user spent *looking at* the order is not money spent *doing*
the order, and a bill that mixes them answers neither question. A class whose dollars are
zero and whose count and wall clock are not is the honest rendering of a mechanical path.
Call that out on the surface, because a reviewer will otherwise read a `0.00` line as a
bug.

**What it must not do.** Accounting is an observer — `agent_usage`'s own module docstring
says so. A failed meter row never fails the report it was measuring, and never fails the
work order. `bill.py`'s settlement seal and its existing arithmetic are unchanged: this is
a new kind arriving through a path the bill already has, not new maths. The gate is never
consulted to decide whether a READ may proceed.

**Absent is not zero, here too.** An order that ran before this landed has no observability
rows. The class renders as *not recorded*, never as `0.00` spent — §2's standing rule
governs this section's own numbers exactly as it governs every other section's.

---

## Agent profile

You are a Jarvis OS engineer working on the order-observability feature. You are building
one slice of it; other workers are building the others in parallel, and you will never see
their sessions.

**What you must know about this codebase.**

It is a stdlib-only Python core — argparse, sqlite3, json — in `src/jarvis/`, about 50
modules. Imports run strictly downward: leaves (`paths`, `db`, `catalog`, `claude_cli`,
`timeline`, `probes`, `remedies`, `testing`) → stores (`central_store`, `project_store`,
`neo_store`) → adapters → `dispatch`/`ops` → `daemon`/`cli`/`ui`. `cli.py` imports every
jarvis module lazily, inside function bodies. There are three SQLite databases: `os.db`
(central), `neo.db`, and one per project at `<project>/.jarvis/jarvis.db`. No module calls
`sqlite3.connect` directly; everything goes through `db.connect`.

Serena is activated and the code map is committed. **Read `.serena/memories/codebase-map.md`
and `work-order-lifecycle.md` before you explore the tree**, and use `find_symbol` and
`find_referencing_symbols` rather than grepping for symbols. Rediscovering the architecture
is the most expensive thing you can do with your context, and it has already been written
down for you.

**The conventions you must follow.**

Business logic lives in `ops.py` and returns plain dicts. The CLI and the dashboard both
consume that dict verbatim and neither computes anything of its own. A renderer that
derives a number is a renderer the other surface will eventually disagree with; that is a
bug this codebase has shipped before.

Comments in this tree explain *why*, at length, and frequently cite the issue number or
knowledge-base id that forced the decision. Match that density — it is the house style, not
decoration. Code comments obey the house style otherwise: compressed, no filler.

Tests live in `tests/`, run with `uv run pytest tests/ evals/`, and use the fixtures in
`src/jarvis/testing.py` plus the fake `claude` executable so nothing touches the real CLI.
Run `uv sync --extra dev` first in a fresh worktree. A new database column is untested
until one test writes it and another reads a row that predates it.

You work in a git worktree, you open your own pull request against `main`, and you never
commit to `main`. Merging, releasing and restarting services are gated: ask before you are
blocked, with `jarvis gate request`.

**The traps in this specific area, and they have all bitten before.**

*Absent is never zero.* A transcript that has expired, an order that predates a new
column, a turn with no API call — each is reported as absent with a sentence saying so.
Printing `0` for any of them is a false claim, and it is literally issue #227.

*A remainder is not a measurement.* If you compute something by subtraction, label it as
the residual it is. Every layer above you will otherwise treat it as observed fact.

*A proxy defers to its authority.* `invariants.check_prefix_stable` is the authoritative
measurement of prefix stability. Any signal you add in that area names a cause and defers;
it does not raise a competing alarm.

*Never synthesise a verdict from a failure.* If a model call, a delivery or a read fails,
the work stays pending and retryable and the surface says it was **unreachable** — never
that it was decided, escalated or refused. A crash dressed as a decision costs the user
attention and teaches them to distrust the surface.

*Bound and redact anything you did not write.* Tool inputs, prompts and transcript text
carry file contents and secrets. Cap them, redact them through a named tested function, and
state the cap in the payload.

*A subagent is a partition of its parent turn, never an addition to it.* Breaking this
makes two surfaces disagree about the same order's cost.

**What you must never do.**

Do not rewrite `inspection.py`'s existing walk, `usage.py`'s pricing, or `bill.py`. Do not
add a hook, a spans table, or any persistence beyond the one column §5 names. Do not add a
new alarm kind. Do not take an acting path — this feature explains, `remedies.py` acts. Do
not widen your scope into a sibling's section because it looked small; your section is your
job and the seams were chosen deliberately.
