# Neo observability

Work order wo-327f211c. Neo's question volume, outcomes, escalation causes, token spend
and latency — captured where it is missing, aggregated in one report, rendered by a CLI
command and a dashboard page.

## The problem

**Neo is the only agent in the OS with no report.** Workers have `jarvis cost`, `jarvis
inspect`, `jarvis alarms`, `jarvis issues`; the knowledge base has `jarvis learn stats`
(`ops.knowledge_usage_report`, src/jarvis/ops.py:13404). Neo has `jarvis neo list` /
`show` / `learnings` / `export` (src/jarvis/cli.py:1352-1380) — four listings of
individual rows, no aggregate of any kind. `NeoStore.counts()`
(src/jarvis/neo_store.py:601) is the closest thing that exists and it is a `GROUP BY
status` over all time with no project, kind, window or rate in it, built for the
dashboard's nav badge.

Consequence, stated by the user: Neo has been escalating the majority of assumptions
lately and there is no surface that would show it, let alone show whether the trend is
rising or what it is escalating FOR.

Most of the data is already in the stores; two things are genuinely not captured.

**Capture gap 1 — no latency on any Neo call.** `agent_calls`
(src/jarvis/central_store.py:206-230) has `ts`, model, token classes, `cost_usd`,
`prompt_chars`, `ok` — and no duration column. The only latency recorded anywhere in the
OS is `panel_opinions.latency_ms` (src/jarvis/neo_store.py:200), written by
`panel._record` from a `time.monotonic()` bracket in `panel.py:607,621`, and the panel
**ships disabled** (`catalog.PanelConfig.enabled` default false, `mem:neo-panel`). So on
every fleet running the shipped default, Neo's latency is recorded nowhere, for any kind
of call. The duration is measurable and thrown away: `claude_cli.run_headless_result`
calls `_run(...)` at src/jarvis/claude_cli.py:1888 and returns without timing it.

**Capture gap 2 — no groupable escalation cause.** `drain_queue`'s escalate branch
(src/jarvis/neo.py:474-476) writes `store.mark(q["id"], "escalated",
reason=verdict["reason"])`, and `reason` is whatever prose the model put in its `reason`
field — "one line why the user must decide", per every persona. It is free text, it is
the same column the transport failures write their `UNREACHABLE_PREFIX` string into
(src/jarvis/neo_store.py:120, `release_claim`, `hold_claim`, `reclaim_stale`), and it is
one of the fields `search_questions` scores. Nothing can group it, so the question the
user actually has — *why* is Neo handing these back — has no answer in the data.

The same column conflating two facts is already a known defect fixed once:
`UNREACHABLE_PREFIX`'s docstring says `failed` "is one status covering two facts" and the
prefix is the word surfaces read to tell them apart. A prefix match is what a report must
not be built on.

**Status `failed` vs `escalated` is not the chosen/failed split.** `failed` is only the
*exhausted* transport case. `neo._unparseable_verdict` (src/jarvis/neo.py:301) synthesises
`{"escalate": True}` from unreadable output, and `daemon.py:6756` marks a question
`escalated` when `autoreview.read_ruling` force-escalates an acceptance whose `stakes` was
missing or unreadable — both land in `escalated` beside genuine model decisions. So
counting `escalated` as "Neo chose to escalate" overstates Neo's judgement by an unknown
amount today.

## The fix

One new report function, one new CLI command, one new dashboard page, two new columns, one
new closed enum. Nothing new is stored that an existing table can hold.

### 1. `questions.escalation_cause` — nullable, closed enum, never derived from prose

New entry in `neo_store.ADDED_COLUMNS["questions"]` (src/jarvis/neo_store.py:211),
`"escalation_cause": "TEXT"`. NULL on every pre-existing row and on every row where the
cause is not known — which renders "not recorded", per Neo's ruling on question 1161.
Nullable and NOT `NOT NULL DEFAULT ''` deliberately: the sibling columns in that dict use
`''`/`0` defaults and document them as "not recorded", but this column is read by a
GROUP BY and an empty-string bucket beside eight real causes reads as a ninth cause.

The vocabulary lives in **src/jarvis/neo_store.py**, beside `Q_KINDS`, `Q_STATUSES` and
`SEATS`, for the reason that module's own comment gives for `SEATS`: the catalog
validator, the CLI and the report all need it and none should depend on another. Two
tuples, because the two classes are different kinds of fact and a single tuple invites a
report that adds them up:

```python
ESCALATION_CAUSES_CHOSEN = (...)   # the model picked this label
ESCALATION_CAUSES_FAILED = (...)   # the OS derived this, mechanically
ESCALATION_CAUSES = ESCALATION_CAUSES_CHOSEN + ESCALATION_CAUSES_FAILED
```

**AMENDED, Neo's ruling on question 1170 — there are THREE tuples, not two.** Two classes
cannot express the largest escalation population: four sites in `daemon.py` re-mark a
question `escalated` AFTER Neo ANSWERED, because the OS overrode the answer, and that is a
third answer to "who decided". So a third tuple ships:

```python
ESCALATION_CAUSES_OVERRIDDEN = ("stakes-high", "stakes-unclassified",
                                "stakes-unreadable", "neo-denied", "scope-over-cap")
```

Five members, every one derived mechanically by `autoreview.escalation_cause` (one pure
helper, so the four call sites cannot drift) from `Ruling.overridden`, `Ruling.stakes`,
`Ruling.accept`, the verdict's own `escalate` and the plan path's child cap. Never read off
a reply: no persona offers one, and `neo.parse_verdict` drops one a model names.
`stakes-unclassified` MOVES here out of FAILED — the OS overriding an acceptance is not
"Neo never answered".

The same ruling DROPS `classifier-unreachable` and `classifier-unparseable`:
`stakes.HIGH_UNREACHABLE` / `HIGH_UNPARSEABLE` reach `autoreview.HELD_HIGH_STAKES`, which
holds the review before any question row exists, so nothing could ever write them. A class
nothing can write is better documented as invisible than left in the enum, so the report
NAMES it instead — `ops.NEO_ESCALATION_INVISIBLE_NOTE`, rendered by the CLI and the page as
what the reader cannot see here rather than as a zero.

`causes` therefore has four keys: `{"chosen": {...}, "overridden": {...},
"failed": {...}, "not_recorded": n}`, and a cause in none of the three tuples still counts
into `not_recorded`. Both renderers show the three classes as three labelled groups, each
carrying the sentence that says what it means for who decided.

The site this does NOT label: `_deliver_assumption_verdict`'s settle-time drop
(`still.armed` false). Neo's ruling ACCEPTED there — the escalation was caused by the
condition table re-running, which `still.code` records on the work order — so the cause is
NULL. A member of the enum would claim a stakes word or a denial that did not happen.

**Chosen — each member is tied to the persona text that asks for it. No member exists that
no persona mentions.**

| member | the prose it comes from |
|---|---|
| `high-stakes` | `neo.PERSONA`:46-48 ("production systems or live credentials; spending money; deleting or publishing anything; legal/people matters"); `autoreview.ASSUMPTION_REVIEWER_PERSONA`:1225 ("CHANGES SOMETHING THE USER WOULD RECOGNISE") |
| `no-learning-applies` | `neo.PERSONA`:48 ("a genuine preference you have no learning about and cannot infer"); autoreview:1229 ("a preference you have no learning about") |
| `conflicting-authority` | `plans.PLAN_REVIEWER_PERSONA`:453 ("You cannot reconcile the plan with a standing learning below") |
| `ambiguous-intent` | plans:454 ("two reasonable decompositions differ, and picking one is really picking what the user meant") |
| `scope-too-large` | plans:448 ("at or over the child cap") |
| `privileged-action` | plans:451 ("Any child would need one of the project's privileged actions"); `gates.REVIEWER_PERSONA`:747 ("genuinely torn about a REAL privileged action") |
| `evidence-insufficient` | gates:726 ("Tests or checks are failing, absent, or not mentioned at all"), :736 ("does not add up, including a claim you cannot check"); `supervisor.ALARM_REVIEWER_PERSONA`:174 ("evidence so thin that any answer you gave would be a guess"); autoreview:1258 ("your ruling would turn on a result you cannot see") |
| `user-decision` | supervisor:176 ("a decision about the WORK, and only the user takes those") |

Nothing for the general `question` kind beyond `high-stakes` and `no-learning-applies`:
those are the only two clauses `neo.PERSONA` states.

**Failed — derived by the OS at the code path, never read off a model's reply.**

| member | the code that writes it |
|---|---|
| `transport-unreachable` | `NeoStore.release_claim` when `attempts >= max_attempts` (src/jarvis/neo_store.py:431) — the `UNREACHABLE_PREFIX` row, reached from `drain_queue`'s `ClaudeCliError` clause (neo.py:462) |
| `attempts-exhausted` | `NeoStore.reclaim_stale`'s `failed` UPDATE (neo_store.py:355) — `MAX_ANSWER_ATTEMPTS` spent on a stranded claim |
| `prompt-refused` | `drain_queue`'s `PromptTooLargeError` / `InputTooLargeError` clauses (neo.py:435,449), both `release_claim(..., max_attempts=0)`; also `NeoStore.ask`'s `QuestionTooLargeError` where a row exists |
| `unparseable-reply` | `neo._unparseable_verdict` (neo.py:301), i.e. `structured.coerce`'s `on_invalid` after `structured.InvalidOutput` |

**FOUR members, per the amendment above** (`neo_store.ESCALATION_CAUSES_FAILED`):
`classifier-unreachable` and `classifier-unparseable` were DROPPED — nothing can write them
— and `stakes-unclassified` MOVED to `ESCALATION_CAUSES_OVERRIDDEN`, because Neo DID answer
there and the OS overrode the answer. The FAILED class means Neo never answered at all, and
that is also why the report gives it a rate of its own: `unreachable_rate`, never folded
into `escalation_rate` (§4).

**Overridden — derived by `autoreview.escalation_cause`, never read off a reply.**

| member | what the OS held |
|---|---|
| `stakes-high` | Neo accepted and flagged it high; the OS obeyed |
| `stakes-unclassified` | `autoreview.read_ruling` (autoreview.py:1167-1177): an acceptance whose `stakes` is missing or empty, force-escalated by the `ROUTINE_STAKES` allowlist |
| `stakes-unreadable` | a stakes word the OS cannot read as routine |
| `neo-denied` | `verdict: deny`, and there is no machine rejection |
| `scope-over-cap` | children at or over `plans.CHILD_CAP` |

`hold_claim` (neo_store.py:393, usage limit) writes **no cause**: it returns the question
to `queued` and escalates nothing. A window that will reopen is not an escalation.

### 2. The `cause` field in the verdict, and the one rule about it

`neo._validate_verdict` (src/jarvis/neo.py:265) gains one key, read exactly as `stakes`
and `exempt_pattern` already are — normalised, never trusted:

```python
"cause": _escalation_cause(data.get("cause")),   # "" when absent or not in the enum
```

`_escalation_cause` lowercases, strips, and returns the member if it is in
`ESCALATION_CAUSES_CHOSEN`, else `""`. **It never raises and the verdict is never
rejected for it.** `_validate_verdict` raises only on a missing `escalate`, because that
field is what makes the reply a Neo verdict at all; a missing `cause` is a label absent
from an otherwise valid decision, and raising would turn it into
`_unparseable_verdict` — an escalation the model never made, which is question 388's bug
re-introduced one field along. `""` stores as NULL and renders "not recorded".

Only `ESCALATION_CAUSES_CHOSEN` is accepted here. A model naming `transport-unreachable`
is describing something it cannot know about; that reads as `""`.

`_unparseable_verdict` (neo.py:301) returns `"cause": "unparseable-reply"` — the only
place in `neo.py` where a failed-class member is written, and it is written by the OS in
the fail-safe, not copied from a reply.

`drain_queue`'s escalate branch passes it through:
`store.mark(q["id"], "escalated", reason=verdict["reason"], cause=verdict.get("cause") or "")`.

`NeoStore.mark` gains `cause: str = ""`, asserted `in ESCALATION_CAUSES or == ""` (the
same `assert status in Q_STATUSES` discipline one line above), and written with the
existing `COALESCE(NULLIF(?,''), escalation_cause)` idiom so a later `mark` never erases a
cause already recorded. `release_claim` / `reclaim_stale` / the `PromptTooLarge` paths set
theirs in their own UPDATEs beside the `answer_reason` they already write.

**Each persona's STRICT JSON block gains `cause`, listing only its own permitted
members**, in the module that owns the kind (`neo.PERSONA`, `gates.REVIEWER_PERSONA`,
`plans.PLAN_REVIEWER_PERSONA`, `supervisor.ALARM_REVIEWER_PERSONA`,
`autoreview.ASSUMPTION_REVIEWER_PERSONA`) — `neo.build_system_prompt`:173-180 is the map
of which goes where. The enum is one shared closed set so the grouping cannot drift; each
persona lists a subset so no persona is told to pick a label its mandate never mentions.
Wording, every time: *on an escalation, name the cause from this list; omit it if none
fits* — never "pick the closest", which manufactures a label.

**No backfill of `escalation_cause`, at all.** Every historical escalation has only prose.
A regex over `answer_reason` is exactly what Neo's ruling on question 1161 refused, and a
guessed label is worse than NULL because it cannot be told apart from a measured one.
Every OTHER metric in this spec backfills in full, because every other metric reads
columns that already exist.

### 3. `agent_calls.latency_ms`, end to end

1. **Measure** in `claude_cli.run_headless_result`: `started = time.monotonic()`
   immediately before the `_run(...)` at src/jarvis/claude_cli.py:1888,
   `latency_ms = int((time.monotonic() - started) * 1000)` immediately after. The same
   bracket `panel.py:607` already uses, moved down one layer so every OS call gets it and
   not only the panel's. Inside the `ExitStack` but around the subprocess only: argument
   assembly and JSON parsing are not the model's time. A call that raises
   `ClaudeCliError` produces no `HeadlessResult` and so no row — the gap
   `run_headless_result`'s docstring already declares for `prompt_chars`.
2. **Carry** on `HeadlessResult` as a new field `latency_ms: int = 0`
   (src/jarvis/claude_cli.py:1618), and injected into the envelope beside
   `prompt_chars` at claude_cli.py:1919 (`result.usage["latency_ms"] = latency_ms`).
   Both, for the reason stated there: `agent_usage.record` takes either a
   `HeadlessResult` or an envelope, and the sites that hand over `usage=result.usage`
   (`structured.request`, so `digest.summarise`; `panel._record`) only ever see the
   envelope. One without the other silently records zero for a whole class of calls.
3. **Record**: `agent_usage.record` reads `latency_ms` off the dataclass in the
   `isinstance(usage, claude_cli.HeadlessResult)` branch (agent_usage.py:197-210) and off
   the dict in the fallback beneath it (:213), exactly as `prompt_chars` is read in both,
   and passes `latency_ms=` to `add_agent_call`.
4. **Store**: `central_store.ADDED_COLUMNS["agent_calls"]` (central_store.py:478) gains
   `"latency_ms": "INTEGER"` — **nullable**, unlike `prompt_chars` beside it. Diverging
   from that convention on purpose: 0 chars of prompt is impossible so `0` can safely mean
   "not measured" there, whereas a sub-millisecond call rounds to 0 and the report must not
   print "0 ms" for a call nobody timed. `add_agent_call` (central_store.py:1708) takes
   `latency_ms: int | None = None` and writes it; `derive_turn_usage` is untouched — a
   worker turn's envelope cannot honestly carry one (claude_cli.py:1914's rule).

`panel_opinions.latency_ms` stays as it is (`NOT NULL DEFAULT 0`, per seat). It measures a
different thing — one seat of a round — and the report reads the `agent_calls` column, so
there is no second measurement of the same call and no migration of that table.

### 4. `ops.neo_stats_report` — one dict, named keys

New function in **src/jarvis/ops.py**, placed with the other report builders: after
`knowledge_usage_report` (:13404), which is the closest analogue in shape and the one it
mirrors (`project` + `days` in, one plain dict out, no rendering). It reads **both** DBs —
`NeoStore` for `questions`, `CentralStore` for `agent_calls` — and joins them on
`agent_calls.question_id = questions.id`, which is the link `add_agent_call` has recorded
since `agent_calls` existed and `neo.answer_question`:362 populates on every Neo call.

```python
def neo_stats_report(project: str | None = None, days: int | None = None,
                     limit: int = 20) -> dict[str, Any]
```

```
{
  "scope": project or "fleet", "days": days, "since": ts | None,
  "questions": {"asked", "answered", "escalated", "failed", "open", "superseded",
                "escalation_rate": float | None, "unreachable_rate": float | None},
  "by_kind":    {kind:    {"asked","answered","escalated","failed",
                           "escalation_rate","unreachable_rate"}},
  "by_project": {project: {"asked","answered","escalated","failed",
                           "escalation_rate","unreachable_rate"}},
  "by_day":     [{"day": "YYYY-MM-DD", "asked","answered","escalated","failed",
                  "escalation_rate","unreachable_rate"}],      # oldest first
  "by_kind_note": NEO_ASSUMPTION_KIND_NOTE,
  # FOUR keys, one per class of "who decided" plus the backlog — see §1's amendment
  "causes": {"chosen": {cause: n}, "overridden": {cause: n}, "failed": {cause: n},
             "not_recorded": n},
  "causes_note": NEO_ESCALATION_INVISIBLE_NOTE,
  "per_order": {"work_orders": n, "questions_per_wo": float | None,
                "feature_orders": n, "questions_per_fo": float | None},
  "spend": {"totals":     {"calls","input","cache_write","cache_read","output",
                           "recorded_cost_usd","list_cost_usd"},
            "by_kind":    {agent_calls_kind: {same}},
            "by_project": {project: {same}},
            "by_day":     [{"day": ..., **same}]},
  "latency": {"by_kind": {kind: {"calls","measured","p50_ms","p90_ms","max_ms"}},
              "unmeasured": n},
  "floor": True, "floor_reason": COST_FLOOR_NOTE,
}
```

- `escalated` counts `status='escalated'`; `failed` counts `status='failed'`; `answered`
  counts `status='answered' AND answered_by='neo'`. `superseded` is `answered_by='os'`,
  broken out and **excluded from the rate's denominator**: `NeoStore.supersede`'s
  docstring says the decision was taken somewhere else, so it is neither Neo answering nor
  Neo handing back. `open` is `NEO_HELD_Q_STATUSES`.
- `escalation_rate = escalated / (answered + escalated + failed)`, over settled questions
  only. Open ones have no outcome yet and including them would make the rate fall whenever
  the queue is busy.
- `unreachable_rate = failed / (answered + escalated + failed)` — its own figure, never
  blended into `escalation_rate`, because a crash is not a decision: a question Neo was
  never reached for reads as UNREACHABLE and never as escalated, the same rule
  `jarvis status` and `jarvis neo list` already state. Folding it in also destroyed the
  chosen-vs-failed split this report exists for, and made a day of transport failures draw
  a bar labelled "rate 100%" beside "escalated 0". Both rates carry through every bucket —
  `questions`, `by_kind`, `by_project`, `by_day` — and both are `None`, never `0.0`, on an
  empty denominator.
- `by_day` buckets `ts` with `strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')` — SQLite
  does the bucketing, as `agent_call_totals` already sums in SQL. A day inside the window
  with no questions appears with zeroes (see the zero rule below); days outside it do not
  appear.
- `causes` is a `GROUP BY escalation_cause` over `status IN ('escalated','failed')`, split
  into the three dicts by membership of the three tuples. `not_recorded` is the NULL count.
  A value in none of them (a release that removed a member) is counted into `not_recorded`
  rather than silently inventing a bucket.
- **Reuse, do not re-derive, the pricing.** `spend` is built from
  `CentralStore.agent_call_totals(project)` grouped by `kind/label/model` and priced per
  group through the same `ops._priced_group` / `_call_spend` path `cost_report` uses
  (ops.py:11661-11700). `recorded_cost_usd` is the CLI's own figure summed;
  `list_cost_usd` is the same tokens at list prices. Two currencies, not blended — `_os_spend`'s
  rule. `agent_call_totals` has no `ts` filter today, so `by_day` and the window need a
  new sibling query on the same table keyed by `(kind, project, day, model)`; it goes in
  `central_store.py` beside `agent_call_totals` and not in `ops`, because no module above
  the store writes SQL.
- Which kinds count as Neo: `neo_answer`, `panel_seat`, `validation_seat`,
  `stakes_classifier`, `digest`, `supervisor`, `health` — the subset of
  `agent_usage.KIND_LABELS` the lead enumerated. Excluded: `COMPACTION`,
  `WORKER_SUBPROCESS` and every member of `agent_usage.OBSERVABILITY_KINDS` (those are the
  worker's own spend and the user's looking, by that module's own classes). The subset is
  named as a frozenset in `agent_usage.py` — `NEO_KINDS` — beside `SUBPROCESS_KINDS` and
  `OBSERVABILITY_KINDS`, so it is a fourth class in the module that owns the vocabulary and
  not a literal list in `ops`.
- `latency` percentiles are computed in Python over the window's rows (`measured` = rows
  with a non-NULL `latency_ms`). No SQL percentile: SQLite has none, and the row count here
  is bounded by the window.

**The denominators for `per_order`.** `ops.registered_project_paths()` → one
`ProjectStore` per project in scope, counting work orders with `created_at >= since` via
`list_work_orders(limit=…, include_hidden=True)` (hidden included, `cost_report`'s
reason: hiding is a gesture about attention and the order still asked its questions), and
feature orders via `list_feature_orders(kind="feature", limit=…)` — `kind="feature"` and
not `None`, because `None` folds improvement orders in and the metric asked for is per
FEATURE order. The numerator is questions whose `wo_id` belongs to one of those orders;
`kind='triage'` questions are **excluded from both numerators** and from `per_order`
entirely, since `Q_KINDS`' comment says a triage question has no work order behind it and
its `wo_id` is empty — dividing them by an order count would be arithmetic over two
different populations. They stay in `questions` and `by_kind`.

**The zero rule, and it is two rules.** Absent renders "not recorded", measured zero
renders `0` — `agent_usage.OBSERVABILITY_KINDS`' comment ("A zero that was measured is not
an absent figure") is the existing statement of this and it holds here with a line down
the middle:

- `questions` is a **census**. Every question Neo was ever asked has a row, so a count of
  zero is measured and prints `0`: "0 escalated" is a fact. Only the *ratios* can be
  absent — `escalation_rate`, `unreachable_rate`, `questions_per_wo`, `questions_per_fo`
  are `None` (never
  `0.0`) when their denominator is zero, and the renderers print "not recorded". Zero
  settled questions and zero escalations are different answers.
- `spend` and `latency` are a **sample**, floored. `agent_usage.record` never raises, so a
  missing row is possible and the figures are a floor — `floor`/`floor_reason` carry
  `COST_FLOOR_NOTE` verbatim, as `cost_report` does. A kind with no measured latency gets
  `p50_ms: None`, not `0`; `latency.unmeasured` is the count of rows that predate
  §3 and renders as its own line so the reader can see how much of the window is blind.
- `causes.not_recorded` is the third shape: every pre-`escalation_cause` escalation lands
  there, and on a live instance it will be the largest bucket for the first weeks. The
  renderers say *"N escalations predate cause recording"* rather than printing it as a
  cause, so nobody reads the backlog as a finding about Neo.

### 5. `jarvis neo stats [--project p] [--days n] [--json]`

New `ne.add_parser("stats", …)` in the existing `neo` subparser group at
src/jarvis/cli.py:1352, dispatched from `cmd_neo`. Renderer `_print_neo_stats(res,
as_json)` placed beside `_print_knowledge_usage` (cli.py:4157) and built the same way:
`--json` prints the ops dict unchanged and returns; otherwise a person's rendering, in the
order the user asked the questions — volume and outcomes, the escalation trend, the
causes, the kinds, per-order averages, then spend and latency last. `--days` defaults to
`None` meaning all time, as `learn stats` does.

The trend is a sparkline-free text series: one line per day, `asked / answered /
escalated` and the rate, newest last so a rising column reads downward. No chart in the
terminal; the dashboard does that.

### 6. `/neo/stats`, a page and not a section of `/neo`

A **new read-only page**, linked from `/neo`'s header, rendering
`ui/templates/neo_stats.html` with `active="neo"` so the existing nav entry
(base.html:231) stays highlighted and **no new top-level nav entry is added**.

Why not a section of `/neo`: that page is an action surface — four tabs, each with a form,
plus `_question.html`'s `answer_form` macro — and kn-a7e321bc / kn-c609211f say a gate
escalation is *also* a Neo question (`kind='approval'`) and no surface may imply it can be
decided on the Neo page. A statistics block showing `approval` and `assumption` counts
sitting among review forms is precisely that implication. kn-9758d35a points the same way:
assumption reviews live on the work order page, not Neo's tab. A separate URL also keeps
`neo_page`'s existing load (every question plus an `opinions` lookup per question,
app.py:1503-1506) from growing a second full pass over the same table.

Route beside `neo_question_page` (src/jarvis/ui/app.py:1516): `@app.get("/neo/stats")`,
reading `?project=` and `?days=` query parameters, calling `ops.neo_stats_report` and
passing the dict straight to the template. The route does no arithmetic — `ops` holds the
report and the page holds the rendering, which is how `/cost` and `bill.html` are split.

Rendering rules:

- Dollars through `_bill.html`'s `money()` macro (imported, not re-written). Token columns
  copy `cost.html`'s existing columns and order (input / cache write / cache read /
  output) — no second token-table style.
- "not recorded" as `<span class="sub">not recorded</span>`, the exact idiom
  bill.html:128 already uses for an unmeasured figure.
- The escalation trend is a per-day bar row reusing the inline-div bar pattern at
  bill.html:122-126 — same scale down the column, so a rising rate is visible as shape.
- No new macro file. A macro is introduced only if a block is rendered on two surfaces,
  and nothing here is: the CLI renders from the same dict in Python.

### What this does NOT do

- **No new table and no new question kind.** Everything is two columns on existing tables.
- **"Early-reading confirmations" cannot be broken out, and the report says so.** The ask
  lists them as a question type. They are `kind='assumption'` like any other, and the only
  record of which pass filed one is the `early` flag on the project store's
  `autoreview_asked` event, read by `Daemon._asked_early` (daemon.py:6704) through a scan
  of that order's events. Splitting the kind breakdown on it means opening every project DB
  and walking events per question — a cost this report does not pay for one sub-bucket. The
  kind breakdown is over `Q_KINDS` and `neo_stats.by_kind` carries an explicit note that
  `assumption` covers both passes. Making it groupable is a separate change (a column on
  `questions`, written by `autoreview.propose`) and is NOT in this work order.
- **No escalation-cause backfill**, per §2.
- **No change to what Neo decides.** The personas gain one optional output field. A verdict
  with no `cause` behaves today's way, byte for byte, in every other respect.
- **No alarm or threshold on the escalation rate.** The user asked to SEE the trend. An
  attention item for a rising rate is a policy decision with a number in it, and the number
  should be chosen after looking at the first weeks of this report.

### Rejected alternatives

1. **Regex the cause out of `answer_reason` and backfill.** The obvious fix, and the one a
   reviewer will propose because it makes the historical data non-empty. Refused by Neo on
   question 1161 (option (a) confirmed): a cause must be a label the model picked, not a
   matcher's guess at prose. A guessed label is indistinguishable from a recorded one and
   contaminates the exact grouping the feature exists to provide. NULL costs the user a few
   weeks of history; a wrong label costs them the metric.
2. **A `cause` column with `NOT NULL DEFAULT 'unknown'`.** Cheaper to query and it makes
   "not recorded" a value the GROUP BY returns for free. Refused: a sentinel string in the
   same domain as real members is read as a member by every consumer that was not told
   otherwise, which is the bug `autoreview.ROUTINE_STAKES` was written to fix after it
   shipped once.
3. **Reject a verdict whose `cause` is missing or unrecognised.** Would guarantee the
   column is populated. Refused: `structured.coerce`'s `on_invalid` is
   `_unparseable_verdict`, so rejecting turns a perfectly good decision into a synthetic
   escalation the model never made — question 388's bug, one field along. The spec's rule
   is the inverse: `escalate` is the only field whose absence is a bad shape.
4. **A new `neo_stats` / `neo_metrics` table written as questions settle.** Would make the
   report a single cheap SELECT. Refused: every figure except the two capture gaps is
   derivable from `questions` and `agent_calls`, a derived table cannot be backfilled for
   anything it was not recording, and it introduces a second source of truth for counts
   that `NeoStore.counts()` already reads one way.
5. **Put latency on `panel_opinions` only, where a column already exists.** Refused: the
   panel ships disabled, so that measures nothing on a default fleet, and it cannot see a
   `neo_answer`, a `digest` or a `stakes_classifier` call at all.
6. **Measure latency in `agent_usage.record` (wall clock at the recording site).** Refused:
   `record` is called after the reply is parsed and, for the `recorder()` sink, from a
   different stack entirely — it would time the OS's own bookkeeping as part of the model's
   latency. The transport is the only place the subprocess boundary exists.
7. **A section on `/neo`, not a new page.** Refused for kn-a7e321bc / kn-c609211f: see §6.

### Test plan

New file **tests/test_neo_stats.py** — the report's own behaviours, against a seeded
`NeoStore` + `CentralStore` (no model, no daemon):

1. counts and the per-kind / per-project / per-day splits over a known fixture, including
   that `superseded` (`answered_by='os'`) is out of the rate's denominator and that
   `kind='triage'` is out of `per_order`;
2. `escalation_rate`, `unreachable_rate` and both `questions_per_*` are `None` — not
   `0.0` — with a zero denominator, and a question count of zero is `0`; a window of only
   unreachable questions is 0% escalation and 100% unreachable;
3. the cause split: a chosen member lands in `causes.chosen`, an override in
   `causes.overridden`, a never-answered one in `causes.failed`, a NULL in
   `not_recorded`, and the four never add into each other;
4. `--days` windowing excludes older rows from every section including `spend`;
5. latency: a window of rows with no `latency_ms` yields `p50_ms is None` and a non-zero
   `unmeasured`, never `0`.

Extensions to existing files, each where that behaviour already lives:

- **tests/test_neo.py** — `parse_verdict` normalises a valid `cause`, returns `""` for a
  missing one, an unknown one and a FAILED-class member named by the model, and in every
  one of those cases the verdict is still the verdict (no `UNPARSEABLE_PREFIX`);
  `_unparseable_verdict` carries `unparseable-reply`; `drain_queue`'s escalate branch
  writes the cause to the row and its `ClaudeCliError` / `PromptTooLargeError` clauses
  write theirs. The persona assertion is on the SHIPPED prose: every member of
  `ESCALATION_CAUSES_CHOSEN` appears in at least one persona string, and no persona names
  a member outside the enum — `mem:neo-panel`'s rule that prose is asserted against what
  the runtime reads.
- **tests/test_neo_store_panel.py** or a `mark`-focused case in tests/test_neo.py —
  `mark` rejects a cause outside the enum, and a second `mark` does not erase a recorded
  one.
- **tests/test_schema_upgrade.py** — both ADDED_COLUMNS entries ALTER onto a database
  created without them, and pre-existing rows read NULL (this file already covers the
  post-release-column mechanism for both stores).
- **tests/test_agent_usage.py** — `record` lifts `latency_ms` off a `HeadlessResult` AND
  off a bare envelope dict, and passes `None` when neither carries one (assert on what
  would be written, via the `store=`/`record=` seam, per that module's docstring).
- **tests/test_claude_cli.py** — `run_headless_result` returns a non-None `latency_ms` and
  injects it into `result.usage` beside `prompt_chars`, against the fake `claude`.
- **tests/test_ui.py** — `/neo/stats` renders 200 with an empty store and prints "not
  recorded" rather than a zero for an absent ratio; `/neo` is unchanged and carries the
  link. (tests/test_ui_cost.py is the model for asserting on a rendered token table.)
- **tests/test_cli.py** (or the existing home of the `neo` subparser cases) — `jarvis neo
  stats --json` prints the ops dict unchanged, and the human rendering of an empty fleet
  contains no `0%` where the rate is `None`.

Run ONLY the targeted files above — the ones this work touches or adds:

```bash
uv run pytest tests/test_neo_stats.py tests/test_neo.py tests/test_ui.py \
  tests/test_agent_usage.py tests/test_claude_cli.py tests/test_schema_upgrade.py -q
```

**Do NOT run the full suite locally.** Pinned user ruling kn-356c724b (`jarvis learn
search "DO NOT RUN THE FULL TEST SUITE" --project jarvis_os`): the suite takes ~21
minutes, a worker turn's prompt cache lives 5, so every full run re-sends the whole
conversation at the cache-write rate — and CI already runs `pytest tests -q` plus
`pytest evals -q` and the browser tests on every pull request, which is strictly more.
Cite CI for the suite.
