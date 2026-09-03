# The supervisor: an agent that answers the alarm before the user has to

Feature order `fo-a10521d8`. Extends the live cost alarm shipped by PR 159
(`docs/superpowers/specs/2026-08-30-the-anatomy-of-a-turn.md` §6).

## Why

PR 159 gave the OS an alarm that fires WHILE a turn is still costing money — a turn past
`alarm_turn_minutes`, a subagent join open past the cache TTL, a re-write over
`alarm_write_tokens`. It reaches the user the only way anything reaches the user: the
attention list. That was the right first move and it is also the whole problem. An alarm
is a *symptom*, and the user is the one who has to open the work order, read
`jarvis inspect`, and decide whether 74 minutes on a design document is normal or whether
something has gone wrong. That is exactly the attention the OS exists to spend on the
user's behalf.

The supervisor is the delegate for that decision, standing to the alarm as Neo stands to
a worker's question. It is invoked when an alarm is raised, it reads what is actually
going on in the order, and it does one of two things:

1. **ack** — this spend is explicable. Put the attention flag down, tell the user in one
   line what it was and why it was fine.
2. **escalate to Neo** — the supervisor cannot settle it. Neo either hands back advice
   the supervisor records and acts on, or escalates to the user, which is where the
   alarm would have gone anyway.

And, because an alarm that only exists on one page is a decision nobody can audit, the
alarm becomes a first-class object in the work order's record — with an id, a link, a
place in the timeline, a place in the conversation, and a review the user can correct.
That correction is the supervisor's memory: the next alarm is judged by an agent that
has been told what the user thought of the last one.

## Architecture at a glance

| Piece | Where |
|---|---|
| the alarm row | `wo_alarms` in `<project>/.jarvis/jarvis.db`, beside `wo_events` |
| the raise | `Daemon.check_burning_turns` (`src/jarvis/daemon.py`), unchanged in shape |
| the agent | `src/jarvis/supervisor.py`, one strict-JSON headless call |
| the tick | `Daemon.supervisor_tick`, its own single-thread pool and drain guard |
| escalation | a 4th `neo_store.Q_KINDS` entry, `alarm`, and its own `deliver()` branch |
| its memory | `learnings` in `neo.db`, `seat="supervisor"` |
| the surfaces | `/alarms`, `jarvis alarms`, the work-order timeline and conversation, the PR body |

**The record of an alarm is project state; what the supervisor learned from it is OS
state.** That split is the one architectural decision everything else follows from, and
it is the split Neo already runs on.

## The seven pieces

Each numbered section below is one work order. A section is self-contained on purpose:
its worker sees that section and its own brief, and nothing else.

---

## 1 — The alarm becomes a record

**Goal: every alarm has a durable id and a row, and every surface that reads alarms today
renders byte-identically.** No supervisor, no Neo, no model call. This is the foundation
every other piece stands on, and it is useful on its own: PR 159 shipped an alarm with no
identity, so "link to the specific alarm" is not expressible today.

### What exists now

`Daemon.check_burning_turns` (`src/jarvis/daemon.py`, in the reconcile block) reads each
running work order's transcript, calls `inspection.live_alarms(...)` which returns
`inspection.Alarm(kind, reason)` objects, and for each alarm not already on record writes:

```python
store.add_event(wo["id"], "cost_alarm", {"kind": alarm.kind, "seq": turn["seq"],
                                         "reason": alarm.reason})
```

then flags attention with the first alarm's reason. The dedupe memory is those events:
`store.events_of_kind(wo_id, "cost_alarm")`, matched on `(kind, seq)` from the payload.

`ops.list_cost_alarms(project_name, limit)` (`src/jarvis/ops.py`) reads them back
fleet-wide via `ProjectStore.events_across("cost_alarm", limit)` — a single
`JOIN wo_events e JOIN work_orders w` per project that brings the title, status, hidden
and `needs_attention` along. Its ten keys are consumed by `cli.cmd_alarms`,
`ui/app.py:alarms_page`, `ui/app.py:alarm_badge` and `ui/templates/alarms.html`.

### The table

New table in the per-project DB, created in `ProjectStore.SCHEMA` and migrated by the
existing `_migrate()` / `ADDED_COLUMNS` machinery.

```sql
CREATE TABLE IF NOT EXISTS wo_alarms (
  id TEXT PRIMARY KEY,                    -- 'al-' + db.new_id(), like wo-/fo-
  wo_id TEXT NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,                     -- long-turn | long-join | big-rewrite
  seq INTEGER NOT NULL,                   -- the turn it judged
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'raised',  -- raised|reviewing|acked|escalated|skipped|failed
  claimed_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  verdict TEXT, verdict_reason TEXT, note TEXT, decided_at REAL,
  neo_question_id INTEGER,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',   -- unreviewed|approved|corrected
  review_feedback TEXT, reviewed_at REAL
);
```

**Every column past `reason` is created here and left NULL/default.** Sections 2, 3, 5
and 6 fill them. Creating them all now is what lets those four pieces be built at the
same time instead of one after another, so do not "only add the columns this piece
uses".

Why the per-project DB and not `neo.db`, where Neo's questions live: an alarm row is
unreadable without the work order's title, status, hidden flag and attention flag, and
those are `work_orders` columns in this database. A row in `neo.db` could not join to
them (`questions.project`/`questions.wo_id` are loose strings with no foreign key), so
the fleet-wide read would keep its per-project fan-out AND gain a second database.
`ON DELETE CASCADE` also makes `ProjectStore.delete_work_order` erase alarms for free;
a cross-database pointer would need hand-maintained cleanup, which `neo_store.supersede`
exists because the OS has already got wrong once.

### Store API

On `ProjectStore`: `add_alarm(wo_id, kind, seq, reason) -> dict` (mints the id),
`get_alarm(alarm_id)`, `alarms_of(wo_id)`, `alarms_across(limit=200, statuses=None)`
(modelled on `events_across`: joins `work_orders` for title/status/hidden/
needs_attention, NEWEST first), and `update_alarm(alarm_id, **fields)` following
`update_work_order`'s shape.

`alarms_across` must return the same work-order columns `events_across` does, because
`ops.list_cost_alarms` builds its dict from them.

### The raise, rewired

`check_burning_turns` calls `store.add_alarm(...)` for each fresh alarm AND keeps writing
the `cost_alarm` event with its existing payload, plus one additive `alarm_id` key.

**THE DEDUPE IS THE TRAP THAT BREAKS THIS PIECE SILENTLY.** The `(kind, seq)` match
against `cost_alarm` events is what makes it one alarm per turn per kind. If the event
loses `kind` or `seq`, or the dedupe is moved half-way, the same alarm re-raises on every
reconcile tick for the life of the turn — an attention flag that comes back the instant
the user puts it down, which is precisely the "cost alarm becomes wallpaper" failure §6.3
of the PR 159 spec was written to prevent. Either keep the event dedupe exactly as it is
and make `alarm_id` purely additive, or move the dedupe to `wo_alarms` in the same
commit. A single-tick test cannot see this failure — but **the two-tick test already
exists**: `tests/test_inspection.py::test_a_burning_turn_reaches_the_user_the_way_
everything_else_does` runs `check_burning_turns` twice against the same unchanged running
turn with a `clear_attention` in between, and asserts one `cost_alarm` event. **Extend
that test with the `wo_alarms` row count** rather than writing a near-duplicate beside it.

### Backfill

`_migrate()` backfills every existing `cost_alarm` event into a `wo_alarms` row at
`status='skipped'` — see below. A permanent union read ("rows, plus events with no row")
in the one function every alarm surface is built on is worse than a backfill. The
production fleet holds 2 such events today, so this is cheap.

**"Once" is not free, and the obvious test for it is vacuous.** `_migrate()` runs inside
`ProjectStore.__init__` — that is every CLI invocation and every reconcile of every
project, not once per release. So the backfill needs a real guard: an insert keyed on
`NOT EXISTS (wo_id, kind, seq)`, or a marker in the schema-version machinery. A test that
opens the store once and counts rows proves nothing; **construct `ProjectStore` over the
same path three times and assert the count is still one row per legacy event.** Build the
legacy database the way `tests/test_schema_upgrade.py` does — raw `sqlite3.connect` plus
`executescript` over `tests/data/schema-jarvis-0.1.11.sql`, then open it with today's
`ProjectStore`.

**Backfilled rows get `status='skipped'`, not `'raised'`.** `raised` is the supervisor's
work queue (section 2). A backfill that leaves history in it means the first tick after
the supervisor is ever enabled spends one model call per historical alarm across the
whole fleet, on turns that finished weeks ago. `skipped` means "never offered to the
supervisor", renders on the record exactly as it does today, and is also the status the
supervisor tick uses for an alarm it declines to judge.

### The read contract, frozen here

`ops.list_cost_alarms` **must keep all ten keys it returns today** — `project`, `wo_id`,
`title`, `status`, `hidden`, `ts`, `kind`, `seq`, `reason`, `live` — because
`ui/templates/alarms.html`, `cli.cmd_alarms` and `ui/app.py:alarm_badge` all bind them,
and sections 4, 5 and 7 are written against them at the same time as this one. Add, and
do not rename: `id`, `alarm_status`, `verdict`, `note`, `review_status`,
`neo_question_id`. `live` stays what it is — a property of the ORDER's attention flag,
not of the row.

`jarvis alarms` gains `--wo <wo-id>`, filtering to one work order. That is the read a
worker makes when it writes its pull request (section 7), so it must exist here.

### Event vocabulary, frozen here

These four kinds are the contract between this piece and sections 2, 3 and 4. Define them
here (a module constant or the docstring of `add_alarm` — somewhere a reader of this
table finds them); sections 2 and 3 write them; section 4 renders them.

| kind | payload | written by |
|---|---|---|
| `cost_alarm` | `{kind, seq, reason, alarm_id}` — first three UNCHANGED | the raise |
| `alarm_reviewed` | `{alarm_id, verdict, reason, note}` | section 2 |
| `alarm_escalated` | `{alarm_id, neo_question_id}` | section 3 |
| `alarm_advice` | `{alarm_id, neo_question_id, answer}` | section 3 |

### The cascade, and the test that will not notice it

`ON DELETE CASCADE` is why `ProjectStore.delete_work_order` erases alarms for free — but
that function deletes six child tables explicitly and returns a counts dict, and
`tests/test_wo_hide_delete.py::test_delete_work_order_cascades` asserts that dict with
`==` over exactly those six keys. **So a cascade-based implementation leaves that test
green and the cascade untested.** Rely on the cascade (do not add a seventh key, which
would break the existing assertion for no gain) and add a positive assertion after the
delete: `store.alarms_of(wo_id) == []` plus a raw `SELECT COUNT(*) FROM wo_alarms`.

### What must not change

The four surface assertions in `tests/test_inspection.py` must still pass verbatim:
`action="/wo/proj_a/{wo_id}/ack"`, `name="back" value="alarms"`,
`alarms<span class="nav-badge">1</span>`, and the `"nothing is burning"` /
`"1 asking for you"` strings from `cli.main(["alarms"])`. `live` stays derived from the
work order's `needs_attention`, never from `wo_alarms.status`.

### Done

`jarvis alarms`, `/alarms` and the dashboard's alarm badge render exactly as they do
today, from rows instead of from events; every alarm has an `al-` id;
`jarvis alarms --wo <wo-id>` exits 0 and lists only that order's alarms with their ids;
the extended two-tick test asserts one row and one event; opening the store three times
over a legacy database backfills once; deleting a work order leaves no `wo_alarms` row.

---

## 2 — The supervisor agent, and the tick that runs it

**Goal: an alarm the supervisor judges explicable is acked with a note to the user;
everything else is left exactly as it is today.** The module and its daemon tick are ONE
piece: the module's only caller is the tick, the evidence packet's shape is decided by
what the daemon can hand it, and the fail-safe is only testable through the tick.

Escalation is section 3's job. Here, `decision: "escalate"` means *record that it wanted
Neo and leave the attention flag up* — which degrades exactly to today's behaviour and is
an honest shipping state.

### Config: `catalog.SupervisorConfig`

Modelled on `catalog.NeoConfig`, mounted at `os.supervisor`, with field-level
project inheritance exactly as `_parse_inspect` / `_parse_validation` do it (a project
naming one key keeps the OS answer for the rest, and `ProjectSpec.supervisor` is fully
resolved so no caller consults two objects).

```python
enabled: bool = False        # SHIPS OFF — see below
model: str = "opus"
timeout: int = 300
learnings_limit: int = 50
max_age_hours: int = 24      # an alarm older than this is skipped, never judged
```

Two more numbers live in `supervisor.py` as module constants, because they are about the
claim machinery rather than about policy — `STALE_REVIEWING_SECONDS = 900` and
`MAX_REVIEW_ATTEMPTS = 3`, the values and the names `neo_store` already uses for the same
job. **The stale cutoff MUST exceed `timeout`**, or a claim is reclaimed out from under a
call that is still running and one alarm is judged twice. `neo_store` states that relation
in a comment and pins it with a test; do the same.

Two one-line registrations that are easy to miss and both show up as user-visible defects:
`agent_usage.KIND_LABELS` gains a `"supervisor"` entry (without it `jarvis cost` prints a
bare kind), and `catalog.SAFETY_KEYS` gains `"os.supervisor.enabled"` beside the
`"os.neo.enabled"` already there — turning the supervisor off fleet-wide is the same class
of act as turning Neo off.

**A dataclass used via `field(default_factory=X)` must be DEFINED ABOVE its user in
`catalog.py`.** `ProjectSpec` is above `ValidationConfig`'s original position and moving
it is what that piece cost; put `SupervisorConfig` above `ProjectSpec`.

Do NOT put any of this inside `InspectConfig`. That block holds thresholds, and
`tests/test_inspection.py::test_nothing_in_the_module_hard_codes_a_threshold` AST-walks
`inspection.py` for numeric literals — nothing supervisor-shaped may put a number there.

### `src/jarvis/supervisor.py`

`SUPERVISOR_PERSONA` as a module constant, matching `gates.REVIEWER_PERSONA` and
`plans.PLAN_REVIEWER_PERSONA` rather than an asset file — those are the two existing
personas of this exact shape.

```python
def build_system_prompt(store, project, learnings_limit=50) -> str
def build_evidence(pstore, wo, alarm) -> str
def review(pstore, neo_store, wo, alarm, model, timeout, record=None) -> dict
```

`build_system_prompt` is byte-stable per project, like `neo.build_system_prompt`, so
consecutive calls share a cached prefix. In this piece it renders the persona plus an
empty learnings block; section 6 fills it.

**The evidence packet is read-only and cheap.** The supervisor is judging a turn that is
still burning, and a slow instrument is part of the problem. Compose it from what the OS
already has in hand:

- the alarm: kind, reason, turn seq, and how long the alarm has been standing
- the work order: id, title, status, model, the description's first ~500 chars
- `inspection.read_session(wo["session_id"], cfg)` — the same read `live_alarms` already
  makes, rendered as the per-turn split (generating / blocked / tools, the named joins,
  the labelled writes). This is the "inspect what is going on in the order" the feature
  asks for and it costs no model call.
- the last 3 `timeline.build_conversation` turns, each clipped to 400 characters — what
  the worker last said it was doing.

**It does NOT include the worker's transcript verbatim.** The alarm is often *about* a
300k re-write; pasting the conversation into the judge's prompt would make the instrument
one of the largest calls the OS makes. Give the packet a hard ceiling —
`EVIDENCE_BUDGET_CHARS = 8000`, clipped with a stated omission — and pin it with a test
that builds a packet from a deliberately huge session and asserts
`len(build_evidence(...)) < EVIDENCE_BUDGET_CHARS`. `worker_brief.CORE_BUDGET_CHARS` is
the precedent for both the constant and the test.

### The verdict contract

Write this shape into the persona and validate it. Sections 3 and 5 both read it.

```json
{"decision": "ack" | "escalate",
 "reason": "why, one or two sentences — for the record",
 "note": "what the user is told; <= 200 chars; empty when escalating",
 "question": "what to ask Neo; empty when acking"}
```

`_validate` **raises when `decision` is absent**, mirroring `neo._validate_verdict`:
that field is what says this is a supervisor verdict at all rather than some other JSON
the model happened to emit, so its absence is a bad shape and not a default.

Call it through `structured.request(..., attempts=1, on_invalid=_failed_verdict)` —
Neo's fail-safe shape, not the panel chair's retry shape. **`_failed_verdict` escalates.**
Output nobody can read must never become an ack: an ack makes a burning turn invisible,
which is a strict regression on what PR 159 shipped.

**`on_invalid` DOES NOT COVER EVERY FAILURE, and assuming it does ships a tick that
raises out of its own thread.** `structured.request`'s docstring is explicit: transport
failures — `claude_cli.ClaudeCliError` — propagate untouched, because a call that never
happened is not invalid output. So the `failed` state on a transport error is the
caller's own `try/except`, not the fallback. Three separate tests, each asserting
`status='failed'`, the reason on the row and the attention flag still up: (a) the model
returns non-JSON, (b) the transport raises `ClaudeCliError`, (c) the model returns valid
JSON with `decision` absent, which `_validate` must raise on rather than default.

Record the call: `agent_usage.record(kind="supervisor", project=…, wo_id=…, model=…,
usage=…)`, so `jarvis cost <wo-id>` shows what the supervisor spent on that order.

### `Daemon.supervisor_tick`

Mirror `Daemon.neo_tick` (`src/jarvis/daemon.py`): a check on `enabled`, a
`supervisor_draining` guard, a submit to a dedicated `ThreadPoolExecutor(max_workers=1)`,
and a done-callback that lowers the guard.

**Do not run the model call on the daemon's main thread.** `check_burning_turns` is
called synchronously inside the per-project loop; a model call there stalls the tick for
every project in the fleet. **And do not reuse the Neo thread.** The seats already run
inside the daemon's single Neo thread, so the whole question FIFO waits on the slowest
seat; adding a supervisor drain there makes a slow supervisor delay every worker's
question.

The drain claims work with a `claim_next_alarm()` / `reclaim_stale_alarms()` pair on
`ProjectStore` — `raised` → `reviewing` with `claimed_at`, and `reviewing` older than
`STALE_REVIEWING_SECONDS` back to `raised` with `attempts` incremented, giving up at
`MAX_REVIEW_ATTEMPTS` (out of the queue, not looping). **The claim and the reclaim are one
piece and must ship together.** Neo shipped `claim_next` without `reclaim_stale` and a
daemon restart mid-drain parked a question for ever.

Testing the reclaim: asserting `claim_next_alarm()` returns `None` twice is vacuous. Patch
`jarvis.db.now` **while creating** the claimed row so `claimed_at` is genuinely in the
past, then run `reclaim_stale_alarms()` on the real clock. Use
`with monkeypatch.context() as m:` — a bare `monkeypatch.undo()` reverts the `jarvis_home`
and `catalog_file` fixtures too and every surface then renders as an empty OS with no
error anywhere.

Which alarms are judged:

- `status='raised'` only.
- Not older than `max_age_hours`. An alarm about spend the user can no longer prevent is
  the noise the whole mechanism was tuned to avoid.
- On any work order status. **An alarm on an order that has since settled is still
  judged** — the spend is a fact and the user still deserves the note — but see below.
- Anything skipped is moved to `status='skipped'` with a reason, never left in the queue.

### The ack

On `decision == "ack"`:

1. `store.update_alarm(id, status='acked', verdict='ack', verdict_reason=…, note=…,
   decided_at=…)`
2. `store.add_event(wo_id, "alarm_reviewed", {alarm_id, verdict, reason, note})`
3. put the flag down **through `ops.ack_attention`, never `store.clear_attention`**
4. `central.add_inbox(project=…, level="info", title=f"Supervisor cleared an alarm on
   {wo_id}", body=note, wo_id=…)` — the notification the user gets instead of an attention
   item. Inbox rows reach every sink including Telegram, so the title is user-facing copy
   and is specified here rather than left to the worker.

**The ack test that looks right and grades nothing:** `assert needs_attention == 0` passes
just as well if `ProjectStore.clear_attention` was used, which is the exact regression
this forbids. The assertion that discriminates: give the order a prior user
acknowledgement, let the supervisor ack, and assert the earlier `acknowledged_blockers`
value is **still on the row**. Add a second case — an order with a pending assumption,
where `ops.ack_attention` raises `OpsError` and the alarm is still recorded `acked` with
the flag left up.

On (3): `ProjectStore.clear_attention` wipes `acknowledged_blockers` — "any ack against
it is spent" — so a supervisor using it silently discards the user's OWN earlier
dismissals on that order. `ops.ack_attention` remembers, and it also inherits the refusal
on pending assumptions, which is exactly right: the supervisor must never dismiss an
order that has a decision waiting for the user. When it refuses, record the alarm as
acked anyway and leave the flag: the assumption is the louder ask.

**Verified fact you need and would otherwise assume wrongly:** `invariants.true_blockers`
has NO branch for a live cost alarm — an alarm fires on a `running` order and none of its
branches match that status — so it returns `[]` and `ack_attention(wo_id, [])` records
nothing durable. The flag stays down because nothing re-flags it (the section 1 dedupe is
what guarantees that), NOT because the ack was remembered. **So the supervisor's answer
is recorded on the alarm row, and the row is the memory.** Do not add a `true_blockers`
branch for alarms in this piece: the three attention invariants
(`check_attention_reason_is_true`, `check_no_phantom_attention`,
`check_blocked_work_is_surfaced`) only fire on assumption-shaped reasons and on terminal
or blocked statuses, so a `running` order with an alarm is untouched by all three today,
and giving alarms a blocker branch changes that in ways this feature has not measured.

### What the supervisor may NEVER do, and it belongs in code

The verdict vocabulary is exactly `{ack, escalate}`. `supervisor.py` must not import or
call `worker_session.cancel`, `ops.cancel_work_order`, `ProjectStore.set_status`,
`ops.send_message` or `ProjectStore.queue_message`. It never messages the worker and
never stops a turn. This is the `panel.fast_is_permitted` precedent: a safety rule that
matters lives in Python, not in a prompt. The tempting "helpful" move on a 90-minute turn
is to kill it, and killing a turn destroys work with no other record.

Pin it with an AST walk over `supervisor.py`, modelled on
`tests/test_neo_panel.py::test_neo_never_imports_the_panel` — **but not copied from it.**
That test walks `ast.Import` / `ast.ImportFrom` only, which is decorative here:
`supervisor.py` must legitimately import `ops` for `ack_attention`, so `ops.cancel_work_
order` would sail straight through an import walk. Walk `ast.Attribute.attr` and
`ast.Name.id` for the literal names `cancel`, `cancel_work_order`, `set_status`,
`send_message` and `queue_message`, and keep an import walk for `worker_session`.

### It ships OFF

`enabled = False`. The panel is the precedent and is still off; the failure mode here is
the worst available (a wrong ack makes a burning turn invisible); and a first fleet-wide
boot has not tested the cost. **"Byte-identical when disabled" must be asserted as
BEHAVIOUR, not as the database** — section 1 lands a table and an additive payload key
whether the supervisor is on or not. **And the baseline is the tree as section 1 left it,
not `main`**: section 1 has already moved `jarvis alarms`, `/alarms` and the badge onto
rows, so a worker diffing against `main` will report a false regression. The pin:

- zero model calls (assert on CALL COUNTS — a test that reaches the daemon without
  explicitly enabling the supervisor otherwise passes having exercised nothing)
- zero `agent_calls` rows of kind `supervisor`
- every `verdict` / `review_*` column NULL
- `cost_alarm` events still written with the same `kind`, `seq` and `reason`
- `jarvis alarms` and `/alarms` render the same split and `alarm_badge` the same number

### A non-goal, stated so an eager session does not build it

**Do not build an eval that grades how well the supervisor judges.** There is no labelled
corpus of explicable-versus-not alarms, `evals/llm` is opt-in behind `JARVIS_EVALS_LLM=1`
and costs real tokens, and an eval without a corpus grades a model's mood. The honest
check is the section 5 review loop run by hand over a run of real alarms — which is also
the gate for turning the feature on at all.

### Done

With `os.supervisor.enabled: true`, an alarm the supervisor judges explicable ends the
tick with `wo_alarms.status='acked'`, a non-empty `note`, an `alarm_reviewed` event on the
timeline, `needs_attention == 0` with any earlier `acknowledged_blockers` intact, and
exactly one `agent_calls` row of `kind='supervisor'` for that order. With the catalog
untouched, the identical fixture makes **zero** `claude` invocations and zero such rows,
and every verdict column stays NULL. Each of the three failure shapes leaves the alarm
`failed` with the flag up.

---

## 3 — Escalating to Neo

**Goal: the supervisor can hand an alarm it cannot settle to Neo, Neo can hand back
advice or escalate it to the user, and neither ever reaches the worker.**

### The question kind

Add `alarm` as a 4th entry in `neo_store.Q_KINDS`. Give it its own persona in
`neo.build_system_prompt`'s kind map — today
`{"approval": REVIEWER_PERSONA, "plan": PLAN_REVIEWER_PERSONA}.get(kind, PERSONA)`.
The general answerer persona is told to escalate anything that publishes or touches
production; an alarm is a spend judgement about a running session and needs its own
framing, or every alarm reaches the user and the feature has bought nothing.

**The persona constant lives in `supervisor.py` as `ALARM_REVIEWER_PERSONA`**, imported by
`neo.build_system_prompt` the way `REVIEWER_PERSONA` is imported from `gates` and
`PLAN_REVIEWER_PERSONA` from `plans`. Do not write the prose into `neo.py`.

The supervisor files the question with `neo_store.ask(project, wo_id, question,
context=…, kind="alarm")`, writes `wo_alarms.neo_question_id`, sets
`status='escalated'`, and emits `alarm_escalated` `{alarm_id, neo_question_id}`.

`context=` is the same evidence packet `supervisor.build_evidence` composed, plus the
supervisor's own reasoning and the verdict it could not reach. Neo answers from the
question and its context alone — its calls are headless and it can look nothing up — so a
thin context is a thin answer, and this is the whole value of the escalation.

### `jarvis status` must not offer the wrong command

`ops._neo_attention` filters out `approval` and `plan` questions with a docstring that
says exactly why: both are reported by the thing that carries the decision, and telling
the user to `jarvis neo answer` a question whose real resolution is `jarvis gate approve`
sends them to the wrong command. **That argument applies verbatim to an alarm question**,
whose real resolution is `jarvis alarms review`. Add `alarm` to that filter.

### The deliver branch — this is where the one dangerous bug lives

`Daemon._neo_drain`'s inner `deliver(q, verdict)` branches on `q["kind"]`: `approval` →
`_deliver_gate_verdict`, `plan` → `_deliver_plan_verdict`, then `elif verdict["escalate"]`
→ inbox + `flag_attention`, **`elif pstore` → `pstore.queue_message(wo_id, ANSWER_PREFIX
+ answer, source="neo")`**.

That last branch messages the WORKER. There is no worker question here — the supervisor
asked — and messaging a worker in the middle of an expensive turn re-sends the whole
conversation at the cache-write rate, which is the exact cost the alarm exists to report.

**So the kind and its branch are one piece and must land in the same commit.** A kind with
no branch falls straight through to `queue_message`. Add `_deliver_alarm_verdict`,
modelled on `_deliver_plan_verdict`: look the subject up by `neo_question_id`, check it is
still in the state that made the question meaningful, and drop the verdict otherwise.

- Neo answers → `wo_alarms` gets `verdict='ack'`, `verdict_reason` naming Neo,
  `note` from Neo's answer, `status='acked'`; emit `alarm_advice`
  `{alarm_id, neo_question_id, answer}`; then the same ack path section 2 uses
  (`ops.ack_attention` + an inbox line). Neo's advice ends the alarm; it does not reopen
  a second supervisor call.
- Neo escalates → the existing escalation shape: an inbox row and the attention flag,
  with a body pointing at `jarvis alarms show <al-id>` and the question. The alarm stays
  `escalated` and is the user's.
- Either way, `_dispatch_neo_cleanup` must NOT run for `kind == "alarm"` — the existing
  guard is `if pstore and q.get("kind") not in ("approval", "plan")`; add `alarm` to it.
  A cleanup work order dispatched off a cost observation is a work order nobody asked for.

### The pointer obligation, and the safety net that does NOT currently cover you

Adding a question pointer means inheriting `neo_store.supersede`: an alarm closed any
other way — the user reviews it, the work order is deleted, the order settles and the
alarm is skipped — must close its open question, or the question goes on asking the user
for a ruling nobody can give.

`invariants.check_neo_escalations_are_live` is the OS's net for exactly this, and it
**will not catch an alarm question**: it filters `q["kind"] in ("approval", "plan")` and
skips everything else. So add the branch — an `alarm` entry in that filter and a
`_stale_alarm_question(store, q)` helper beside `_stale_approval_question` and
`_stale_plan_question`, where LIVE means the `wo_alarms` row still has
`status='escalated'` and still points at this question. Test it in
`tests/test_stale_escalations.py` in the shape of
`test_an_orphaned_plan_review_is_closed_on_the_tick`.

That invariant exists because three such questions sat in production attention, the oldest
for a fortnight, and none of them was of kind `question` — a point fix at each close site
is what forgets, and this is what costs one tick instead of for ever.

### Escalate only while the order is open

Neo can advise nothing about a finished session. If the work order is no longer in
`project_store.OPEN_STATUSES` when the supervisor decides to escalate, record the verdict
and reason on the row, set `status='escalated'` and flag the user directly — do not file a
Neo question that has nothing to be asked about.

### Every negative assertion here needs a positive partner

`assert store.queued_messages(wo_id) == []` is green on a drain that never ran. So is
"no cleanup work order was created" when the verdict carried `dispatch: None` — feed a
verdict that DOES carry a dispatch payload and assert `len(store.list_work_orders())` is
unchanged. And `assert hasattr(Daemon, "_deliver_alarm_verdict")` is structural and grades
nothing: drive `Daemon._neo_drain`'s inner `deliver()` with a real `kind='alarm'` question
and a **non-escalating** verdict, then assert no `wo_messages` row and no `neo_answered`
event — that is the branch order that would otherwise fall through to `queue_message`.

Every one of those pairs with a positive assertion in the same test: the alarm row moved
off `escalated`, or the `alarm_advice` event exists.

### Done

An alarm the supervisor escalates becomes a `questions` row in `neo.db` with
`kind='alarm'`, `wo_alarms.neo_question_id` pointing at it, `status='escalated'`, an
`alarm_escalated` event on the timeline, and `queued_messages(wo_id)` empty. When Neo
answers, the alarm reaches `acked` with Neo named in `verdict_reason`, an `alarm_advice`
event lands, an inbox row appears whose body carries `/alarms/<project>/<al-id>` — and
`queued_messages(wo_id)` is **still** empty. `neo.build_system_prompt(store, project)` for
kind `question` is byte-identical to what it returned before the kind map grew.

---

## 4 — The order's own record: timeline and conversation

**Goal: an alarm and its handling appear in the work order's own record, so a reader of
`jarvis wo show` or the work-order page sees it without going to `/alarms`.** This is the
feature's requirements 3 and 4.

Buildable immediately after section 1 and in parallel with sections 2, 3 and 5: the four
event kinds are frozen in section 1, and tests construct them directly rather than
running a supervisor.

### Two readings, and they are not derivable from each other

`src/jarvis/timeline.py` has two builders and both are pure — they never open a store, and
in particular never open Neo's. Keep that true; everything needed is already in the events
and messages the caller loads.

- `build_timeline` — WHAT HAPPENED. Lifecycle. Gets `cost_alarm`, `alarm_reviewed`,
  `alarm_escalated` and `alarm_advice`.
- `build_conversation` — WHAT WAS SAID. Gets the supervisor's **note** and Neo's
  **advice**, and nothing else.

**The split between them is a decision, not an oversight.** A verdict is an event; a note
addressed to the user and Neo's advice are speech. `build_conversation` already carries
the worker's question to Neo (`question_asked`) for exactly this reason — a record built
from the project store alone must show what was asked. Follow it: the supervisor's note
becomes a turn with `who="supervisor → you"`, Neo's advice a turn with
`who="neo → supervisor"`. Neither is a `wo_messages` row, so both come in through the
event branch, like `question_asked`.

### `_describe` — not optional

`timeline.event_level` returns `"signal"` for a kind it does not know, so an unhandled
kind renders as a bare kind name plus a JSON blob and **looks fine on the page**. Every
new kind gets a `_describe` branch returning a `(label, detail)` pair. `cost_alarm`
already has one; the three new kinds need theirs. This has been learned here once
already, on the validation kinds — the comment in `timeline.py` says so.

**So `assert event_level("alarm_reviewed") == "signal"` grades nothing** — it passes
before the branch exists. Copy the shape of
`tests/test_timeline.py::test_the_four_validation_events_read_as_four_different_things`,
which was written for exactly this failure: build all four events with the local `ev()`
helper, assert `len(set(labels)) == 4`, assert `label != entry["kind"]` for each, assert
one specific `detail` string, and assert `event_level("message_delivered") == "debug"` in
the same test so the classifier is proved to still discriminate at all.

### `_ref` — the deep link

`timeline._ref` today resolves a payload's `neo_question_id` to
`{"kind": "neo_question", "id": qid, "label": f"question #{qid}"}` and the dashboard turns
that into `/neo/question/<id>`. Add an `alarm` branch returning
`{"kind": "alarm", "id": "al-…", "label": "alarm al-…"}` for all four alarm kinds, and
resolve it in the dashboard to **`/alarms/{project}/{alarm_id}`**.

That URL is the contract with section 5, which builds the route. Use exactly that shape —
project from page context, the way `/wo/{name}/{wo_id}` already does it — or the two
halves of "link to the specific alarm" will not meet.

**That route does not exist yet and this link will 404 until section 5 lands. That is
expected, and it is not yours to fix.** Assert the exact rendered substring
(`href="/alarms/proj_a/al-`) and stop there — `assert "al-" in page` passes off the id
printed anywhere on the page and grades nothing. The assertion that the link RESOLVES
belongs to section 5, which builds the route.

### `jarvis wo show`

The alarm lines already appear via the timeline. Add the supervisor's standing to the
work order's header the way `config:` and the validation rounds are rendered — one line
when the order has any alarm, with the ids.

**The vocabulary, in full, because the supervisor ships off and `raised` is therefore the
common case** — not the interesting one the example would otherwise suggest:

| row status | reads as |
|---|---|
| `raised` | `raised` |
| `reviewing` | `with the supervisor` |
| `acked` | `acked by the supervisor` |
| `escalated` | `escalated to Neo` |
| `skipped` | `not reviewed` |
| `failed` | `supervisor failed` |

So: `alarms: 2 (1 acked by the supervisor, 1 escalated to Neo) — al-1a2b, al-3c4d`, and
with the supervisor off, `alarms: 1 (1 raised) — al-1a2b`.

`--json` carries **the `wo_alarms` rows in full**, not the `ops.list_cost_alarms` dict —
this is one order's own record, and the dict's fleet-wide join columns (`title`, `status`,
`hidden`) are already on the work order the caller is reading.

### Done

`timeline.build_timeline({}, events, [])` over all four alarm kinds yields four distinct
labels, none equal to its own `kind`, and no `detail` that is a JSON blob; the `cost_alarm`
entry's `ref` is exactly `{"kind": "alarm", "id": "al-…", "label": "alarm al-…"}`;
`/wo/<project>/<wo-id>` renders it as `href="/alarms/<project>/al-…"`.
`timeline.build_conversation(events, [])` over a note-plus-advice pair returns exactly two
turns — assert with the tuple-equality idiom from
`tests/test_timeline.py::test_the_conversation_carries_the_question_the_worker_asked`
(`[(c["kind"], c["who"], c["content"]) for c in convo] == [...]`), which is the only shape
that also catches a *verdict* leaking into the conversation. `jarvis wo show` prints the
alarm line for each status in the table above.

---

## 5 — `/alarms` and the CLI review loop

**Goal: the user can open one specific alarm, see what the supervisor decided and why, and
approve or correct it — in the same motion they already use for Neo's answers.** This is
the feature's requirement 2.

Buildable immediately after section 1 and in parallel with sections 2, 3 and 4: it renders
`ops.list_cost_alarms`' frozen dict, and with every supervisor column NULL it degrades to
exactly the page that exists today.

### The page today

`/alarms` (`ui/app.py:alarms_page` → `ui/templates/alarms.html`) is two halves,
deliberately not one table: **Asking for you**, grouped by work order with one ack button
per order, and **On the record**, every alarm ever raised. The grouping is not a shortcut —
the attention flag carries one sentence, so three alarms on one order were only ever one
ask. That reasoning still holds; keep both halves and both explanations.

### What changes

Three halves instead of two:

1. **Asking for you** — alarms still holding the attention flag, unchanged in shape. Now
   also the place a Neo escalation lands.
2. **Addressed by the supervisor, awaiting your feedback** — `status='acked'` and
   `review_status='unreviewed'`. Each row shows the verdict, the supervisor's reason, its
   note, and — when it went through Neo — Neo's advice with a link to the question. Each
   carries the approve/correct control.
3. **On the record** — as today, with the verdict and review columns added. **This half is
   MEANT to be long and to sit there unacted on**: it is the memory that keeps the alarm
   quiet, and a page that hid it would hide the reason acking works.

### The per-alarm page

`GET /alarms/{project}/{alarm_id}` — the anchor requirements 1 and 4 both need. One alarm
in full: what fired and why, the turn, the supervisor's verdict and reasoning, Neo's
question and answer if it escalated, and the review control.

**A list with no anchor is not a link target.** `/neo` was exactly this and the user hit
it: "review it →" opened on the first unreviewed question rather than the one being read,
and it was reported as two bugs. Do not repeat it.

Two siblings emit links at this exact URL shape and neither can prove they resolve — the
timeline's `ref` and the inbox body of a Neo escalation. **You own that assertion**: fetch
`/alarms/{project}/{alarm_id}` and assert 200 with that alarm's reason on the page.

### Where the controls live

`ui/templates/_question.html` holds `answer_form(q, back)` and `review_form(q, back)` for
Neo's questions, and both `/neo` and `/neo/question/<id>` render them **because two
surfaces rendering the same thing separately is how they come to show different things**.
Follow that: the alarm review control is a macro used by both the list and the per-alarm
page, whether it is a new `_alarm.html` or an addition to `_question.html`.

The redirect follows `_neo_back`: `next: str = Form("")`, honoured only when it is a
same-site path (`startswith("/")` and not `"//"`), because a form field is
attacker-settable. Route both success and `OpsError` through it; `base.html` renders
`?error=` on every page.

### `ops.review_alarm`

Modelled on `ops.neo_review` (`src/jarvis/ops.py`):

```python
def review_alarm(alarm_id, approved: bool, feedback: str = "", project_name=None) -> dict
```

Every refusal before the first write, so a rejected review leaves the row untouched:
a correction with no feedback is refused ("what should the supervisor have decided?"); an
alarm that is not `acked` or `escalated` is refused, naming its status. It sets
`review_status`, `review_feedback`, `reviewed_at`. **It does not write a learning — that
is section 6, and this function is where section 6 adds one line.** It does not message
the worker: unlike a corrected Neo answer, an alarm review corrects the supervisor and has
nothing to tell the worker.

**Reviewing an `escalated` alarm closes a Neo question, and this is the close site nobody
else owns.** When the row carries a `neo_question_id` whose question is still open, call
`neo_store.supersede(qid, …)` with the user's decision as the answer. Skip it and the
question goes on asking the user for a ruling they have already given.

After a refused review assert `review_status` is still `unreviewed` **and** `reviewed_at`
is NULL — the pair is what proves "before the first write" rather than "eventually
consistent".

### CLI — the CLI is the OS

`jarvis alarms show <al-id>` and
`jarvis alarms review <al-id> [--reject] [--feedback "…"]`, beside the existing
`jarvis alarms [project]`. A dashboard page that is the only way to see or do something
the OS knows would be the first exception to prime directive 1.

**`alarms` takes a bare positional `project` today, so adding subcommands to it is a real
design decision and not a formality.** `cli.cmd_alarms` is reached by
`cli.main(["alarms"])` with no arguments and by `jarvis alarms proj_a`, and
`tests/test_inspection.py::test_the_cli_answers_the_same_question_as_the_page` calls the
first form. Whichever way you resolve it — a subparser group with the bare list as the
default, or `show`/`review` as sibling top-level commands — **both existing spellings must
keep working unchanged**, and the test that calls them must pass untouched.

### You are not blocked on the supervisor

Say it plainly because it is not obvious: every supervisor column is fillable directly
with `ProjectStore.update_alarm`, so the whole page, both CLI verbs and every test here are
buildable with the supervisor never enabled and `supervisor.py` not existing. **Seed the
fixtures that way — including the screenshots.** Turning the supervisor on to produce a
screenshot burns a real model call for a picture.

### The tests that will pass for the wrong reason

- **Jinja renders an absent key as empty**, so `assert note in page` is trivially true
  when the note is `""`. Build fixtures with *distinctive* strings — a note like
  `the design doc is long on purpose`, a reason appearing nowhere else — assert those exact
  strings, and add one case where a distinctive string is deliberately absent from the
  context and assert it is not on the page.
- `assert client.get("/alarms").status_code == 200` proves nothing about "degrades to
  today". Assert the second half's heading is **absent** when no `acked`+`unreviewed` row
  exists, and re-assert the four existing strings from
  `tests/test_inspection.py::test_the_alarms_page_lists_the_live_one_and_offers_the_ack`
  verbatim.
- The redirect guard needs parametrising, not one happy case: `/alarms` → `/alarms`;
  `//evil.example` → fallback; `https://evil.example` → fallback. Route an `OpsError`
  through it too and assert the `?error=` flash.
- "It is a macro" is not observable, but this is: fetch `/alarms` and
  `/alarms/<project>/<al-id>` for the same alarm, extract the `<form>` block for that
  alarm id from each, and assert the two are **equal**. That is the property
  `_question.html` exists to guarantee for Neo.

### Done

`GET /alarms/<project>/<al-id>` returns 200 carrying that alarm's verdict, reason and
note; posting the review control with `next=/alarms` returns 303 to `/alarms` and leaves
`review_status='corrected'` with the feedback on the row; a correction with empty feedback
is refused and the row is untouched; `jarvis alarms show <al-id>` prints the same three
facts; `jarvis alarms review <al-id> --reject --feedback "…"` exits 0 and the same command
without `--feedback` exits non-zero; `cli.main(["alarms"])` and `jarvis alarms proj_a`
behave exactly as before. A screenshot of each of the three halves is in the pull request
(a UI change without a screenshot is unreviewed).

---

## 6 — The supervisor's memory

**Goal: a decision the user corrects changes the next decision.** The feature's "that
feedback is used by the supervisor for future decisions". Small, and genuinely last: it
needs both the agent (section 2) and the review control (section 5).

### Where the memory lives

`neo.db`'s existing `learnings` table, scoped by its existing `seat` column with
`seat="supervisor"`. Nothing new is built. `NeoStore.learnings(project, seat="supervisor")`
already returns the global rows PLUS that seat's, already excludes retired entries by
default, and already returns them oldest-first so the block is append-only and the prompt
prefix stays cached. That hands the supervisor `jarvis neo learnings`,
`jarvis neo retract` and the retraction discipline with no new code.

This is the OS-state half of the split this feature runs on: the alarm's record is project
state, what the supervisor learned from it is fleet-wide.

### The trap in the seat vocabulary

`neo_store.SEATS` is the **panel roster vocabulary** — `catalog` refuses to parse a roster
naming a seat that is not in it. **Do not add `"supervisor"` to `SEATS`**, or it becomes a
legal panel seat that a catalog can put on Neo's roster, where it has no seat definition
and no mandate. Add a separate constant beside it —
`LEARNING_SCOPES = SEATS + ("supervisor",)` — and widen `ops.validate_seat` (which
`ops.neo_review` calls) against that instead.

### The three edits

1. `ops.review_alarm` with `approved=False` calls
   `NeoStore.add_learning(<the correction, rendered>, project=…, source="review",
   seat="supervisor")`. Render it with a **new `supervisor.learning_from_review(alarm,
   feedback)`**, not by extending `neo.learning_from_review` — that one takes a Neo
   question dict and an alarm is not one, and widening it to accept both shapes puts a
   branch in the middle of Neo's own review path for no gain. The sentence template:
   *"On a `<kind>` alarm (`<the alarm's reason>`) the supervisor decided `<verdict>`
   because `<its reason>`. The user's ruling: `<feedback>`."*
2. `supervisor.build_system_prompt` renders
   `store.learnings(project, limit=cfg.learnings_limit, seat="supervisor")` through
   `neo.render_learnings`, which already carries the character budget, the oldest-first
   truncation and the "N older learnings not shown" note. Reuse it; do not write a second
   renderer.
3. `jarvis neo learnings --seat supervisor` lists them.

**`learnings(seat="")` must keep returning ONLY global rows.** That default is
load-bearing: `neo.build_system_prompt` calls it that way for the single-agent path, and a
supervisor learning leaking in would silently rewrite a prompt prefix that has to stay
byte-stable.

**But write that pin carefully, because the obvious version is green before you start.**
`NeoStore.learnings` already queries `(seat='' OR seat=?)` with `seat or ""`, so "call
`add_learning(seat='supervisor')`, assert Neo's prompt is unchanged" grades nothing about
this work order. Drive it through the real path instead: capture
`neo.build_system_prompt(store, project)`, call `ops.review_alarm(..., approved=False,
feedback=…)`, then assert in ONE test that Neo's prompt is byte-identical **and** that
`supervisor.build_system_prompt(...)` grew and contains the feedback. That version fails
if `review_alarm` writes the learning with `seat=""` or with a panel seat's name, which is
the defect that actually ships.

Same trap on the vocabulary: `assert "supervisor" not in neo_store.SEATS` is also true
today. The pair that grades: `catalog.parse_catalog` with a Neo roster naming
`"supervisor"` raises `CatalogError`, **and** `ops.validate_seat("supervisor")` does not
raise. That is the whole reason `LEARNING_SCOPES` is a separate constant. Decide too
whether `ops.neo_review(qid, approved=False, seat="supervisor")` is refused by
`validate_seat` or by the existing "did this seat opine on this question" check, and test
whichever you chose — it must still be refused.

### What must not change

`neo.render_learnings`' character budget and its "N older learnings not shown" line apply
to the supervisor's block too — pin it the way
`tests/test_neo_learnings_budget.py::test_a_panel_seat_inherits_the_same_bound` does. The
oldest-first, append-only ordering must hold here as well: add two learnings and assert the
first-built prompt is a **prefix** of the second, which is the property that keeps the
cached prefix valid.

### Done

`ops.review_alarm(<al-id>, approved=False, feedback="…")` writes exactly one `learnings`
row with `seat='supervisor'`; the next `supervisor.build_system_prompt` contains the
rendered ruling; `neo.build_system_prompt` is byte-identical across that call;
`jarvis neo learnings --seat supervisor` lists the row and `jarvis neo retract <id>
--reason "…"` removes it from the supervisor's next prompt.

---

## 7 — The alarm in the pull request

**Goal: the pull request of an order that raised an alarm says so, with a link to that
alarm.** The feature's requirement 1, first bullet. Land this LAST — see the transition
risk below.

### What exists

`hooks.PR_BODY_SECTIONS` is the single source of the `##` headings a PR body must carry:
`Summary`, `Implementation notes`, `Questions asked to Neo`, and the rest.
`hooks.pr_body_problems` requires every one to be **present and non-empty**, and the
PreToolUse hook **DENIES** a `gh pr create` that fails, naming the fix. Two shipped
templates must agree with the constant — `.github/pull_request_template.md` and the
skill's bundled copy at
`src/jarvis/assets/skills/open-a-pull-request/pull_request_template.md` — and
`tests/test_pr_body.py` asserts all three agree.

**That is why this is one work order and not three: any split leaves the suite red.**

### What changes

1. `PR_BODY_SECTIONS` gains `Alarms raised`, **immediately after `Questions asked to
   Neo`** — they are the same kind of fact about how the work was supervised, and the
   position must be exact because
   `test_the_template_carries_exactly_the_sections_the_hook_requires` compares heading
   order with `==`.
2. Both templates gain the heading and its prompt text. **That prompt text must be
   scaffolding, not content** — an HTML comment or a bare `-`, never `None.`.
   `test_the_bare_template_fails_every_section` asserts
   `len(problems) == len(PR_BODY_SECTIONS)`, so a template that ships a section already
   satisfying the emptiness rule silently drops the count and that test goes red.
3. `src/jarvis/assets/skills/open-a-pull-request/SKILL.md` gains a paragraph in "Fill
   every section", written like the Neo one: one bullet per alarm with its `al-` id and
   the link `http://localhost:8787/alarms/<project>/<al-id>` (host and port from the
   worker's own catalog), how to get the list (`jarvis alarms --wo $JARVIS_WO_ID`), and
   `None.` when there were none — an empty section reads as a section you forgot. The
   heading string must appear **verbatim** in the skill:
   `test_the_skill_names_the_hook_and_the_bare_ref_rule` loops every section asserting it.
4. **NOT `dispatch._common_briefing` — that tail now reaches only the PLANNER's prompt**
   (its own docstring says so; a worker's prompt is composed from
   `worker_brief.core_contract` plus `worker_brief.section_index` instead). A line added
   there would be read by planners and never by workers, the exact opposite of the intent.
   Put it in `worker_brief`'s `record` or `contract` section — reachable to a worker via
   `jarvis brief <section>` — or omit it entirely, since the `open-a-pull-request` skill
   already reaches every worker through `--add-dir`. If it goes in the core, mind
   `test_core_contract_is_under_the_budget` (`worker_brief.CORE_BUDGET_CHARS`), and note
   that `test_every_core_command_string_parses` extracts every `` `jarvis …` `` from the
   brief and parses it with `cli.build_parser()` — `jarvis alarms --wo <wo-id>` only
   parses because section 1 shipped that flag.

### The transition risk, and why this lands last

The hook denies. The moment this merges, a worker already running that opens a pull
request gets refused for a section it was never briefed about. Two things bound it:
`worker_session.briefing_for` re-composes the full briefing on **every turn**, so a worker
still running picks the new line up on its next turn; and the hook's denial names the
missing section, so a worker that is refused can fix it and retry. It is still the one
change here that can stall work orders that have nothing to do with this feature, which is
why it goes in after everything else and why its pull request should say so.

The in-flight half is **not unit-testable** — it needs a live fleet at merge time. State
that in the pull request as a manual check: after merge, look at `jarvis wo list` for
`running` orders and expect at most one denied `gh pr create` retry each. What IS testable
is the re-composition itself, in the shape of
`tests/test_stable_prefix.py::test_the_briefing_is_identical_on_every_turn`.

Do not try to make the section conditional on the order having raised an alarm. There is
no conditional-section mechanism in `pr_body_problems` today, and inventing one for this is
a larger change than the section itself.

### The vacuity to avoid

Adding `"Alarms raised": "- al-… …"` to `_body()`'s defaults in `tests/test_pr_body.py`
turns all twelve existing tests green without one line proving the section is REQUIRED.
The version that grades is the shape already at `test_a_missing_section_is_named`:
`_body().replace("## Alarms raised", "## Alarms")`, asserting the exact problem string.

### Done

`hooks.PR_BODY_SECTIONS` contains `"Alarms raised"` at a fixed position; both templates
stay byte-identical to each other and their `##` headings equal that tuple **in order**;
`pr_body_problems` on a body missing the heading returns exactly ``no `## Alarms raised`
section``; on a body whose section holds `None.` it returns `[]`; the bare template still
fails every section; `SKILL.md` contains the heading verbatim plus the literal
`jarvis alarms --wo $JARVIS_WO_ID`.

---

## Out of scope, and filed

- **The supervisor acting on the work order** — sending the worker guidance, or cancelling
  a burning turn. This WAS out of scope here, and the reasoning still holds and is still
  why the gate exists: an agent that can message or kill a running worker on a cost
  heuristic is a much larger blast radius than one that can route an attention item, and
  it needs a gate of its own. It was given one in
  `docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md` §5, which widens
  the vocabulary to `{ack, escalate, propose}` and builds exactly that gate: `propose`
  names an id from a closed registry in `remedies.py` and files a `self_heal` approval,
  and `supervisor.py` still names no acting function — section 2's AST pin is unchanged
  and still passes. Cancelling a turn and setting a status remain excluded there too.
- **The supervisor triaging any other kind of attention item.** Alarms only.
- **Auditing every remaining surface where a Neo question is shown.** In scope: the five
  the user listed, plus `jarvis wo show` and `jarvis status`. Neo questions also surface in
  `/neo`, `/neo/question/<id>`, `jarvis neo list|show`, `ops.os_status`'s Neo attention
  block and the inbox; sweeping all of them is an audit, not this feature.
- **Enabling the supervisor by default**, gated on a run of reviewed alarms with no wrong
  ack — the same gate the panel's default-on is waiting behind.

---

## Agent profile

You are a **Jarvis OS core engineer** working on one piece of the supervisor feature —
the OS-level agent that reviews a cost alarm so the user does not have to. You are working
in the Jarvis OS repository itself (`~/workspace/agentic_os`), which is the OS that
dispatched you. Production runs a separate released checkout; nothing you do here touches
the running fleet.

**What you must know about this codebase before you write a line.**

Serena is activated and the code map is committed. Read the memories first —
`codebase-map`, `work-order-lifecycle`, `neo-panel`, `testing` — with `read_memory`, and
navigate with `find_symbol` / `find_referencing_symbols` / `get_symbols_overview` rather
than grepping for definitions. Do not spawn an exploration subagent to rediscover the
architecture; that is what the memories exist to prevent. Search the knowledge base before
you decide anything (`jarvis learn search "<term>" --project jarvis_os`) — this fleet has
paid for a lot of lessons and they are indexed in your prompt.

The core is **stdlib-only**: argparse, sqlite3, json. No YAML, no new dependencies. Imports
run strictly downward — leaves (`paths`, `db`, `catalog`, `claude_cli`, `timeline`) →
stores (`central_store`, `project_store`, `neo_store`) → adapters → `dispatch`/`ops` →
`daemon`/`cli`/`ui`. There are no import cycles at module-import time; `cli.py` imports
everything lazily inside function bodies and you should match that style.

Three SQLite databases: `$JARVIS_HOME/os.db` (central), `$JARVIS_HOME/neo.db` (Neo's), and
`<project>/.jarvis/jarvis.db` (per project). Every connection goes through `db.connect()`.
`ProjectStore.__init__` runs `_migrate()` applying `ADDED_COLUMNS` — **a new column that is
not in that list raises `no such column` on every live database the moment something
splats it into an UPDATE**, and this has already broken a dispatch path once.

**The conventions you must follow.**

- Business logic lives in `ops.py` and is shared by the CLI and the UI. A dashboard route
  delegates; it does not implement.
- Anything the dashboard can do, the CLI can do. The CLI is the OS (prime directive 1). A
  feature that exists only on a web page is a bug.
- Every model call is recorded through `agent_usage.record` so `jarvis cost` sees it.
- Every threshold a surface judges by belongs in the catalog with field-level per-project
  inheritance, not as a module constant.
- Comment density here is reviewed. Comments explain **why**, decisions taken and rejected,
  and traps — never what the line does. Match the surrounding code; read a neighbouring
  module before you write your first docstring.
- Prefer extending an existing shared function over adding a parallel one. Two surfaces
  rendering the same thing separately is how they come to show different things.

**The traps that have bitten this codebase and will bite you.**

- A dataclass used via `field(default_factory=X)` must be defined ABOVE its user in
  `catalog.py`, or it is a `NameError` at import.
- `timeline.event_level` returns `"signal"` for an unknown event kind, so a kind with no
  `_describe` branch renders as a bare name plus a JSON blob and looks fine. Add the branch.
- `monkeypatch.undo()` inside a test body reverts what the `jarvis_home` and `catalog_file`
  fixtures set up, and every page then renders as an empty OS with no error anywhere. Use
  `with monkeypatch.context() as m:`. To test an old turn, patch `jarvis.db.now` while
  CREATING the row so the timestamp is genuinely in the past, and let the code run on the
  real clock.
- A test that reaches a disabled-by-default feature through the daemon without explicitly
  enabling it exercises the fallback and still gets a perfectly good result. **Assert on
  call counts and on rows, never on "a verdict came back".**
- `structured.request(attempts=1, on_invalid=…)` is the fail-safe shape; `attempts=2,
  on_invalid=None` is a real retry. They are opposite policies — pick deliberately.

**What you must never do.**

- Never write to a SQLite database, a session file or project state directly. Everything
  goes through `ProjectStore` / `NeoStore` / `CentralStore`, and everything user-facing
  goes through `ops`.
- Never make the supervisor act on a work order. It does not message workers, does not
  cancel turns, does not set statuses. The verdict vocabulary is `ack` and `escalate`.
- Never let a failure become an ack. Every unreadable reply, timeout and transport error
  leaves the alarm unresolved and the attention flag up.
- Never commit to `main`, and never `cd` out of your worktree.
- Do not build a sibling's piece. Your section is your scope; if you need something a
  sibling owns, use the interface named in your brief and stop there.

**How you finish.** `uv sync --extra dev` in a fresh worktree, then
`uv run pytest tests/ evals/` — the numbers go in the pull request, not the words "tests
pass". Be honest about that second path: `evals/llm` is opt-in behind `JARVIS_EVALS_LLM=1`
and otherwise collects and skips, so "evals passed" on a default run is evidence of
nothing and the test-evidence table should say `n/a, LLM evals not run` rather than imply
a grade. A UI change without a screenshot is unreviewed: capture the page and put it in the
pull request body. Use the `open-a-pull-request` skill before `gh pr create`; the PR body
hook denies a body missing a section or containing a bare `#N`. Then
`jarvis wo finish --pr <url>`. Record what you learned with
`jarvis learn add "…" --project jarvis_os --topic "<topic>"` before you open the PR, so the
ids exist to cite. Any doubt goes to Neo first — `jarvis wo ask <your-wo-id> "…"` — and any
call you made with no doubt goes on the record with `jarvis wo assume`.
