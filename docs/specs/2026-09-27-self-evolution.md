# Self-evolution: detectors and remedies as data, validated by construction, and an Evolution view

The OS notices its own gaps today and closes each one by hand. Throughout 2026-09-26/27 an
operator ran the same loop over and over: an order stopped moving, somebody read the record,
found the cause, filed an issue, a worker fixed that one cause. Five gaps closed that way in
two days — a stale `panel gave up` hold (#786, #813), an unreachable Neo question shown as
in flight (#788), catch-up merges burning validation rounds (#806), a red `main` nobody
noticed (#793), a release order overtaken mid-CI-wait (#784) — and each fix was a code change
specific to one gap.

That does not scale and it does not compound. The pinned ruling says every gap the OS notices
must be closed so the OS SELF-HEALS it, and the ruling behind `jarvis wo fix` (kn-6c252734,
kn-85265170) says a diagnosis must never be a dead end. This feature is the mechanism that
makes both true for gaps nobody has met yet: a small reviewed ENGINE and a fixed set of remedy
PRIMITIVES live in code, and the RULES that pair a symptom with a remedy live in a database and
grow without a code change. Then the user can see it happening: an Evolution view whose
headline number is the share of stuck orders the OS resolved mechanically rather than by paying
an agent to think, and which should trend up.

**The single cleanest statement of what this is.** `supervisor.py` already has the acting half:
it judges an alarm with a model, proposes a remedy, and a `self_heal` gate decides. This
feature replaces the supervisor's LLM JUDGEMENT for symptoms that are mechanically decidable.
It does not replace, widen or route around its ACTING PATH. A fired rule raises the same alarm
row a probe raises and then does nothing else; everything downstream is unchanged.

When it is all done: an investigation that finds a gap files a fix order carrying a `gap_class`,
a detector and a remedy as DATA; the panel refuses to be paid for that order until a registered
detector and remedy exist and both pass a dry run against the order that triggered the
investigation; the rule ships in `dry_run`, recording what it WOULD have done; once a person
arms it, the reconciler clears that class of gap every tick with no model call; and
`/evolution` shows every rule, knowledge entry, gate exemption and gap class the project has
learned, each linking back to the investigation, fix order, issue and pull request behind it.

## 1. The evidence: what the five gaps have in common

Read the five gaps as data rather than as bugs and they are all one shape: an order in a known
status, held or not progressing longer than that status should take, with one distinguishing
fact on the record, and one mechanical act that would clear it.

| gap | status | distinguishing fact | the act that clears it |
|---|---|---|---|
| stale `panel gave up` hold (#786, #813) | `needs_review` | newest hold names a round the panel has since passed | lower the attention flag, drop the hold |
| unreachable Neo question (#788) | `waiting_input` | the question row is `failed` at `attempts == 0` | requeue the question |
| catch-up round burn (#806) | `waiting_pr_merge` | the judged head is behind the PR head, every intervening commit a clean catch-up | carry the verdict onto the new head |
| red base inherited (#793) | `waiting_pr_merge` | the base's own build is red and the branch's checks inherited it | update the branch from the base once the base goes green |
| overtaken release order (#784) | `pending`/`running` | the release it would ship has already shipped | raise a precise attention item naming `jarvis wo done` |

Every row is a CONDITION over facts the OS already records, plus ONE of a small number of ACTS.
Nothing in the condition column needs a model. Nothing in the act column is new authority. What
is missing is only that today both are written in Python, once per gap, by a worker, in a pull
request. That is what this removes: the condition becomes a row, the act becomes a parameter
naming a primitive, and the code that gets reviewed is the engine, written once.

**The precedent is `gate_rules`.** `src/jarvis/gate_rules.py` holds the matcher;
`central_store.gate_rules` holds the rows; builtins come from `gate_rules.seed_rows()`; learned
rows grow from gate dismissals as data, never as code; `jarvis gate rules` reads them and
`jarvis gate rule-retract` retires one without deleting it. Every structural decision below is
that shape again, and **where this spec is silent, do what `gate_rules` does.**

## 2. What already exists, and the standing rules this must not break

**The reconcile band.** `Daemon.tick` (`src/jarvis/daemon.py:716`) runs a project loop; the
reconcile-cadence block is `if reconcile:` and its last step is `Daemon.check_invariants`, which
calls `invariants.check_project(store, repair=True, slow=…)`. Every step in that block carries a
comment arguing its position, and a reviewer will hold a new one to the same standard.

**A detector may not duplicate an invariant, and this is a hard rule.** `invariants.py` is the
closest relative — mechanical predicates, no model, re-evaluated every reconcile tick, repairing
what is unambiguous — and it is the one real duplication risk in the feature. Where an invariant
already DETECTS a condition, a rule's contribution is the REMEDY, and its condition must key off
the `invariant` timeline event that check already writes rather than re-deriving the predicate.
Without this, `INV-ATTENTION-REASON` and a "stale flag" detector fight over one row.
Relatedly: `invariants.true_blockers` is the single source of truth for
`work_orders.attention_reason` and `INV-ATTENTION-REASON` rewrites any reason it cannot
re-derive. A detector may READ that column; nothing here may write a reason `true_blockers`
cannot produce.

**Per-state timing already landed** (wo-f7b00f9f, commit `a478dbc`). `ops.state_durations` at
`src/jarvis/ops.py:1643` reads the `wo_state_spans` table and takes `wo_id=` or `fo_id=`. Its
`as_dict(now)` carries `current_status_age`, `last_activity_age`, `last_activity_kind` and the
per-status `totals`. That is the timing input for every detector and nothing here recomputes it.

**Holds, automerge codes and waits already exist.** `holds.held(store, wo_id)` and
`holds.by_cause` (`src/jarvis/holds.py:198,340`) give the hold spans; the vocabulary is
`holds.HOLD_CAUSES`. `automerge.HELD_*` is the closed set of automerge codes. `ops.waiting_on`
returns the slug `jarvis wo why` prints. `ProjectStore.count_events` / `events_of_kind` /
`latest_validation_round` give the rest. These are facts, not new work, and
`holds.held`'s own docstring already settles the budget question: it is cheap enough to run per
work order per reconcile tick, which is what `Daemon.check_burning_turns` does with it.

**`health.fingerprint` is the dedupe, and `health.observer_kinds()` is a correctness rule.**
`src/jarvis/health.py:51` produces a deterministic summary of everything about a unit that can
move; two units with the same fingerprint are the same situation. `observer_kinds()` exists
because a fingerprint that counted the events the observation itself writes would MOVE AS A
RESULT OF BEING LOOKED AT, and the dedupe could then never engage. Every event kind this feature
writes must join that excluded set — it folds `project_store.ALARM_EVENT_KINDS`, so adding the
new kinds there makes the fingerprint inherit the exclusion.

**`remedies.py` is the acting module and it is the only one.** Read its docstring before you
touch it. Four independent things refuse an act and any one is enough: the registry is CLOSED
and `tuple(REMEDIES) == SHIPPED_REMEDIES` is asserted; `catalog.RemedyConfig` ships off with an
empty allow-list on every project; a `self_heal` gate grant must be approved, unexpired and
unspent, consumed through `gates.open_gate`; and every acting call lives inside a handler,
pinned by an AST walk in `tests/test_remedies.py` keyed on the enclosing function's name.
**All four survive this feature unchanged.**

**kn-6c252734, load-bearing.** The DB grows RULES, never PRIMITIVES. Adding a rule needs no
code. Adding a primitive is a reviewed diff with tests, a `SHIPPED_REMEDIES` update, and off
`RemedyConfig`'s allow-list by default. A rule naming a primitive not in `REMEDIES` is refused
on insert and again at apply.

**The exclusions in `remedies.py` are a boundary, not a gap.** No cancelling a turn, no
`set_status`, no `wo done`, no `fo resume`, no killing a process. No rule and no primitive may
reach any of them by any spelling.

**`wo_alarms.probe` is one shared namespace.** `probes.RESERVED_IDS` exists because
`inspection`'s alarm kinds and `probes.DEFAULT_PROBES` ids live in that column together — and
`DEFAULT_PROBES` already contains `no-progress`. Detector ids land in the same column, so §6
owes a test that detector ids, probe ids and `inspection`'s alarm kinds are pairwise disjoint.

**Two gates, not a chain** (`docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md`). §7's
mechanical check is the FIRST gate and runs before the panel is paid; the panel is the second
and judges quality. They must not be collapsed, and the mechanical one must never be phrased as
an opinion.

**A review blocks only on blockers** (pinned). §7's injected checklist asks the seats whether
the detector matches the real symptom, whether the remedy is safe and idempotent, and whether
there is a test. It must NOT ask whether the detector EXISTS — that was decided mechanically,
and a seat re-deciding it produces a rejection the submitter cannot act on.

**Never fabricate a default answer from a failure** (pinned). A detector that raises, a snapshot
that cannot be built, a `gh` call that fails, a trigger order that cannot be read: each records
that it was UNREADABLE, leaves the subject alone, and stays retryable. Not a refusal, not a
false positive, not a decision.

**Absent is never zero.** A rule with no fires has no hit rate. A project with no armed rules
has no mechanical share. §9 prints the sentence, never the digit.

**Everything acting ships OFF.** `RemedyConfig` disabled with an empty allow-list, `PanelConfig`
disabled, `validation.auto_review` off. This feature adds `os.rules.enabled`, default **false**,
resolvable per project — the house pattern, and it is asked for if omitted.

**Architecture and conventions.** stdlib-only Python in `src/jarvis/`; imports run strictly
downward (leaves → stores → adapters → `dispatch`/`ops` → `daemon`/`cli`/`ui`); `cli.py` imports
jarvis modules lazily inside function bodies; three SQLite databases, all reached through
`db.connect` and never `sqlite3.connect`; business logic in `ops.py` returning plain dicts that
the CLI and the dashboard both consume verbatim.

## 3. The registry: two tables, the condition grammar, the pure evaluator, and the CLI

`src/jarvis/rules.py` is the new LEAF module: stdlib plus `db`, `catalog` and `remedies`, and
nothing above. It holds the condition grammar, the pure evaluator, the `Facts` dataclass shape,
and the lookup helpers. The rows live in the CENTRAL store, `os.db`, added to
`central_store.SCHEMA` beside `gate_rules`. **This section delivers no firing and no acting.**

**Why central and fleet-wide.** Most gaps are OS behaviour, not project behaviour: a rule
learned on `jarvis_os` should protect every project without being copied into each project's
database. `gate_rules` made the same call for the same reason and its DDL comment argues it. One
difference to state in the DDL comment, because the columns are identically named and do not
mean the same thing: `gate_rules.project` is provenance only, while `detectors.project` is
provenance AND an optional SCOPE — `''` means every project, a name means that one.

### 3.1 Two tables for the rule, not one

A single row carrying condition and remedy together cannot express *"the detector was right and
the remedy failed"*, which is exactly the distinction §8's recurrence ledger must make; and one
detector legitimately accumulates several remedies over time with older ones retracted. It is
also the shape the user described: a detector row, and a remedy row.

```sql
CREATE TABLE IF NOT EXISTS detectors (
    id TEXT PRIMARY KEY,                  -- 'dt-' + db.new_id
    ts REAL NOT NULL,
    gap_class TEXT NOT NULL,              -- slug, probes.ID_PATTERN shape; §8's join key
    project TEXT NOT NULL DEFAULT '',     -- provenance AND optional scope; '' = fleet-wide
    subjects TEXT NOT NULL DEFAULT 'work_order',
    condition TEXT NOT NULL,              -- JSON, the §3.2 grammar
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'dry_run',   -- dry_run | armed | retracted
    source TEXT NOT NULL DEFAULT 'builtin',   -- builtin | io | user
    io_id TEXT NOT NULL DEFAULT '', fix_wo_id TEXT NOT NULL DEFAULT '',
    issue_url TEXT NOT NULL DEFAULT '', pr_url TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0, last_fired REAL, last_cleared REAL,
    false_positives INTEGER NOT NULL DEFAULT 0,
    recurrences INTEGER NOT NULL DEFAULT 0,
    arm_threshold INTEGER,                -- recorded, NEVER acted on; see §3.3
    armed_at REAL, armed_by TEXT NOT NULL DEFAULT '',
    armed_reason TEXT NOT NULL DEFAULT '',
    retired_at REAL, retired_reason TEXT NOT NULL DEFAULT '',
    seed_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS remedy_rules (
    id TEXT PRIMARY KEY,                  -- 'rm-' + db.new_id
    detector_id TEXT NOT NULL REFERENCES detectors(id),
    ts REAL NOT NULL,
    primitive TEXT NOT NULL,              -- a key of remedies.REMEDIES, validated on insert
    params TEXT NOT NULL DEFAULT '{}',
    argument TEXT NOT NULL DEFAULT '',    -- what the gate request says, in words
    status TEXT NOT NULL DEFAULT 'dry_run',
    hits INTEGER NOT NULL DEFAULT 0, last_fired REAL,
    false_positives INTEGER NOT NULL DEFAULT 0,
    retired_at REAL, retired_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rule_fires (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    detector_id TEXT NOT NULL, remedy_rule_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL, order_id TEXT NOT NULL, order_kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,            -- health.fingerprint, the dedupe memory
    mode TEXT NOT NULL,                   -- dry_run | armed
    outcome TEXT NOT NULL,                -- recorded | proposed | applied | refused
                                          --   | unreadable | cleared
    alarm_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    cleared_at REAL, cleared_seconds REAL,
    false_positive INTEGER NOT NULL DEFAULT 0,
    false_positive_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_fires_detector ON rule_fires(detector_id, ts);
CREATE INDEX IF NOT EXISTS idx_rule_fires_order ON rule_fires(order_id, detector_id);
```

`outcome` distinguishes six things that must not be collapsed. `recorded` is a dry run.
`proposed` is an armed fire that raised an alarm; `applied` is one whose remedy the gate then let
run, and the two are separate because §9's headline counts acts, not intentions — an alarm nobody
approved changed nothing. `refused` is an armed fire the remedy path declined — allow-list,
missing grant, precondition — with the refusal's own words in `detail`; a refusal is not a hit
and not a false positive, it is the gate working. `unreadable` is the pinned ruling's case:
something could not be read, nothing was decided, and the row exists so the silence is visible.
`cleared` closes a fire.

The enum ships complete from this section even though only §6 ever writes `proposed`, `applied`
or `refused`: a value a later child adds to a column a shipped release already reads is a
migration, and there is no reason to buy one.

`detail` and any snapshot text stored on a fire is BOUNDED at `FACTS_CHARS` (4000) with the cap
recorded, the way every other payload in this codebase is.

**Dedupe is `health.fingerprint`, against the newest OPEN fire for `(detector_id, order_id)`.**
A condition standing for six hours is one fire, not 720. A fire is CLOSED — `cleared_at` and
`cleared_seconds` written, `detectors.last_cleared` updated — when the condition no longer holds,
and only then may that detector fire on that order again. Commit `0c1e3f9` ("Dedupe a hold
against the newest one, not every past one") already had to learn this once.

### 3.2 The condition grammar

A condition is a JSON document. It is not a code string, not a Python path, and nothing
evaluates it with `eval`.

```json
{"all": [
  {"field": "status", "op": "eq", "value": "needs_review"},
  {"field": "hold_cause", "op": "eq", "value": "panel_gave_up"},
  {"field": "seconds_in_status", "op": "gte", "value": 3600}
]}
```

Combinators `all`, `any`, `not`. Leaves `{"field", "op", "value"}`. Operators `eq`, `ne`, `in`,
`not_in`, `gte`, `lte`, `exists`, `absent`, and `count_gte` over `event_counts` (taking
`{"kind": …, "value": n}`). **There is deliberately no regex operator and no operator over free
text.** `gate_rules.validate_pattern` exists precisely because that registry did admit regexes
and had to be fenced afterwards; this one does not admit them in the first place. A condition
must be reviewable by a person in one reading and reasoned about by §7's pre-check.

`rules.FACT_FIELDS` is a CLOSED table mapping each field name to its type and to the existing
call that supplies it. It is the engine's only extension point needing code, and that is
deliberate: a field nobody implemented must be a write-time refusal, never a silent `None` that
makes a condition vacuously false.

| field | type | source |
|---|---|---|
| `status`, `kind`, `hidden`, `needs_attention`, `attention_reason` | str/bool | the `work_orders` row |
| `seconds_in_status`, `seconds_since_activity`, `lifetime_seconds`, `last_activity_kind` | num/str | `ops.state_durations` |
| `hold_cause`, `hold_seconds` | str/num | `holds.held` / `holds.by_cause`, vocabulary `holds.HOLD_CAUSES` |
| `automerge_code` | str | the `automerge.HELD_*` set |
| `waiting_on` | str | `ops.waiting_on`'s slug |
| `round_no`, `round_outcome`, `rounds_left`, `judged_head_sha` | num/str | `ProjectStore.latest_validation_round` |
| `pr_url`, `pr_state` | str | the RECORDED columns; never a live `gh` call |
| `event_counts` | map kind → int | `ProjectStore.count_events` |
| `invariant_events` | map name → int | the `invariant` timeline events — the §2 rule's route |
| `depends_on_count`, `dead_dependency_count` | int | `invariants.dead_dependencies` |
| `neo_question_status`, `neo_question_attempts` | str/int | `invariants.awaiting_neo` |
| `budget_usd`, `spent_usd` | num | the budget columns |

`rules.parse_condition(raw)` raises `RulesError` carrying EVERY problem at once — the house shape
of `plans.parse_plan` and `findings.parse_report` — refusing an unknown field, an unknown
operator, an operator applied to the wrong type, a document deeper than `MAX_DEPTH` (4) or wider
than `MAX_NODES` (32), and an empty combinator. It runs on insert, so nothing unvalidated reaches
the table, and again on read, because a row written by an older release may name a field a later
one removed; a row that fails on read is reported `unreadable` and never evaluated.

`rules.matches(cond, facts) -> bool` is pure: no DB, no model, no clock it was not handed.

**Absent is not false.** A leaf whose field is absent evaluates FALSE for every operator except
`absent`, and the evaluator records WHICH fields were absent so `jarvis rules dry-run` can say
"this did not match because `hold_cause` is not recorded on this order" rather than merely "no".
A condition that matches only because a field is missing is the commonest way a rule over-fires,
and the dry-run output is where that gets caught.

**Acceptance criterion for the grammar, and it is the one that matters:** all five conditions of
§3.5's seed rules must be expressible in it without extending it. If one is not, the grammar is
wrong — not the seed rule. That is why the seed DATA lives in §3.5 rather than beside the
evaluation pass: a grammar whose stated acceptance criterion cannot be checked by the section that
defines the grammar has no acceptance criterion.

### 3.3 Arming is explicit. The threshold is recorded and never acted on.

`dry_run → armed`, `armed → dry_run`, and either of them `→ retracted`. Nothing else. A detector
is CREATED in `dry_run`, always, whatever created it; there is no argument that writes an armed
one. `retracted` is terminal and `retired_reason` is required.

**`armed → dry_run` exists for exactly one cause: the disarm interlock of §6**, when a person has
marked `FALSE_POSITIVE_DISARM` of that detector's fires wrong. Retracting a misfiring detector
instead would be the wrong verb twice over — it loses the calibration history the `arm_threshold`
column exists to accumulate, and it says the rule was a mistake when what happened is that it is
not ready. A demoted detector keeps firing in `dry_run`, where being wrong is free.

**Only a person arms a rule, in v1.** `arm_threshold` is written and read back and displayed; no
code acts on it. Arming a rule off a counter the OS itself increments is the OS granting itself
acting authority on its own evidence, and there is no hit history yet to calibrate a threshold
against. The column exists so the calibration data accumulates; automating the flip is a
follow-up once it has.

Rows are never deleted and never rewritten in place except the counters, the timestamps and the
arm/retract fields — the same append-mostly discipline as `gate_rules` and the knowledge base,
because what the OS believed and when is evidence.

### 3.4 `CentralStore` methods and the CLI

Mirror `add_gate_rule` / `gate_rules` / `retract_gate_rule` / `record_gate_rule_hit`, retraction
semantics included: `add_detector`, `get_detector`, `list_detectors(*, project="", gap_class="",
status="", include_retired=False)`, `add_remedy_rule`, `remedy_rules_for(detector_id)`,
`arm_detector(id, *, by, reason)`, `retract_detector(id, reason)`, `retract_remedy_rule(id,
reason)`, `record_rule_fire(...)`, `close_rule_fire(...)`, `record_false_positive(fire_id,
reason)`, `detectors_for_gap(gap_class, project="")`, `list_rule_fires(...)`.

**One more public function, and it is a CONTRACT with a caller that does not exist yet.**
`rules.resolve(detectors, remedy_rules, facts) -> tuple[Resolution, ...]` — given one order's
`Facts`, the registered remedies whose detector currently matches it, each `Resolution` carrying
the detector, the primitive, the parameters, the remedy row's `argument`, and the `can_apply`
sentence. It is pure, it acts on nothing, and it is the answer to the question "what does the
registry say could be done about this order right now".

This is the function `jarvis wo fix` will call to resolve a named blocker through the registry
instead of through a closed match table (kn-6c252734, kn-85265170). That command's own remedy
seam is on an unmerged branch (wo-dbea82cf), so the wiring is deliberately NOT in this feature —
but the function it will call is, built in the shape it needs, with `remedies.REMEDIES` as its
only source of primitives. A lookup API that arrives after its caller is a lookup API its caller
worked around, which is why the SHAPE, the return type and the insert-time parameter validation
are fixed here rather than deferred with the wiring.

**Tested here against the three primitives that already ship** — `nudge`, `unblock`,
`file_work_order` — or against stubs. NOT against all five seed rules: six of the primitives
those rules name (`update_branch`, `carry_verdict`, `lower_attention`, `drop_hold`,
`retry_neo_question`, `raise_attention`) do not exist until §4 lands, and this section has no
dependency on §4 — the two are built in parallel on purpose, and adding the edge would serialise
the root of the whole feature. §5.3 owns the resolve proof over all five seed rules, because it
is the one section that depends on both. (Neo, question 878.)

Its own top-level CLI family, **`jarvis rules`**:

```
jarvis rules list [--project p] [--status dry_run|armed|retracted] [--gap-class c]
jarvis rules show <dt-id>
jarvis rules arm <dt-id> --reason "…"           # required
jarvis rules retract <dt-id|rm-id> --reason "…" # required; never deletes
jarvis rules dry-run <dt-id> [<order-id>]       # evaluate now, print, WRITE NOTHING
```

Not folded into `jarvis gate rules`: that family answers "what counts as privileged", and one
verb meaning two registries is how `jarvis gate rules` stops being readable. The collision is
close enough that `jarvis rules --help` must open by saying which registry it is and naming the
other. `list` leads with the counts — `12 rules: 3 armed, 8 in dry run, 1 retracted` — then one
line per detector. `show` renders the condition as prose, every remedy rule with its primitive
and parameters, the whole provenance chain, and the newest fires with their outcomes. Every
`ops` function returns a plain dict §9's dashboard consumes verbatim.

### 3.5 The seed rows

`rules.seed_rows()`, the direct analogue of `gate_rules.seed_rows()`, returns the five detectors
of §1 with their remedy rows — **every one in `dry_run`**, `source="builtin"`, `project=""`,
`gap_class` set, and provenance pointing at the issue that discovered the gap:
`stale-panel-hold` (#786, #813), `unreachable-neo-question` (#788), `catch-up-round-burn` (#806),
`red-base-inherited` (#793), `overtaken-release-order` (#784).

**It is pure data in a leaf module and it lives HERE, beside the grammar, not beside the
evaluation pass.** Two reasons. §3.2's acceptance criterion is that all five conditions are
expressible without extending the grammar, and that is only checkable where the grammar is
defined. And the rows are the CONTRACT every later section reads — §5.3 fires them, §8 joins on
their `gap_class`, §9 counts them — so they must land before any of those, and this is the only
section with no dependency on §4.

**What this section asserts and what it does not.** `tests/test_rules.py` asserts every seed
condition PARSES. It does NOT assert that each `primitive` exists in `remedies.REMEDIES`, nor that
the parameters satisfy its schema, nor that `rules.resolve` returns a `Resolution` for them:
those primitives are §4's and do not exist yet, and a section cannot assert against code that has
not landed. §5.3 owns all three of those and the firing proof.
Neither check is optional; they are simply not in the same place, and each section says so
because neither worker can see the other.

Seeding is invoked by §5.3. Nothing here calls it.

## 4. The primitives: the acts a rule may name

`remedies.REMEDIES` widens in ONE reviewed diff, and every guarantee in its docstring holds
unchanged. This section depends on nothing else in the feature — it is a pure widening of an
existing module, independently testable, and it can be written from day one. Each new entry
carries `headline` and `blast` (the words a reviewer reads and the words the judge was shown are
the same words) and `subjects`.

`nudge`, `unblock` and `file_work_order` are untouched. The new ones, each wrapping an existing
`ops` call rather than reimplementing one:

| id | what it does | idempotent because |
|---|---|---|
| `update_branch` | merge the base into the pull request's branch | a branch already up to date is a no-op |
| `carry_verdict` | record the carried head when the catch-up proof holds | the carry is already recorded |
| `force_rejudge` | open a fresh round reading the current PR, via the path `jarvis validation force` already uses | refuses with a round already open |
| `lower_attention` | clear an attention flag whose reason `true_blockers` no longer re-derives | the flag is already down |
| `drop_hold` | end a hold episode whose stated cause no longer holds | the hold is already ended |
| `retry_neo_question` | requeue a question left `failed` with no attempt spent | the question is already pending |
| `raise_attention` | flag attention with a precise reason naming the command to type | the reason is already that |

Two additions to `Remedy` that the rest of the feature needs:

1. **A parameter schema.** `Remedy.params: tuple[Param, ...]`, each a name, a type and whether it
   is required. `rules` validates a remedy row's `params` against it on insert, so a rule cannot
   name a parameter the primitive does not take nor omit one it needs. A primitive taking none
   declares an empty tuple.
2. **A precondition predicate.** `Remedy.can_apply(pstore, subject, params) -> str`, returning
   `""` when the act is currently possible and a SENTENCE saying why not otherwise. §7's
   mechanical gate calls it and `dry-run` prints it. It is READ-ONLY and it decides nothing about
   authority — the grant, the allow-list and the AST walk still do that, and `can_apply`
   returning `""` authorises nothing.

**Do not reimplement the catch-up proof** (kn-907c9a61). `update_branch` and `carry_verdict`
call the existing one: PARENTAGE from GitHub plus CONTENT hashed locally with `--full-index
--no-ext-diff --no-textconv`, never `patch-id`, keeping the new-side blob id for binary files. A
second implementation of it is a fail-open, and a reviewed one at that.

**`open_investigation` is deliberately NOT here.** It would need
`ops.create_investigation_order`, which is landing separately on an unmerged branch
(wo-4beada49). A primitive that exists and cannot act is worse than one that does not exist,
because a rule can name it. It is a one-entry follow-up once that lands.

**Every new primitive is off by default.** `RemedyConfig`'s allow-list ships empty, so an
upgraded fleet gains the primitives and applies none until somebody allow-lists one — the posture
the first three shipped with, and not a regression to be fixed.

## 5. Evaluation on the tick, and the five seed rules

### 5.1 Where the pass runs

A new `Daemon.rules_tick(project, store)`, inside the `if reconcile:` block and **immediately
AFTER `self.check_invariants(...)`**, which is currently that block's last step.

The position is the whole of the argument, so write it into the comment:
`invariants.check_project(store, repair=True)` REPAIRS what is unambiguous on the same tick, so a
detector pass running before it fires on conditions the OS was about to fix itself — a false
positive manufactured purely by ordering. `check_invariants` is already documented as "Last:
check the state everything above just produced"; this is "and now decide whether any of what is
LEFT is a known gap".

It is **not** an entry in `invariants.INVARIANTS`, and not in `Daemon.health_tick` /
`_health_sweep`. An invariant is a post-condition the OS GUARANTEES; a rule is an admitted
heuristic shipped in `dry_run`, and the two must not read as one claim on the timeline or in
`jarvis doctor`. `check_project(repair=False)` runs through the `_ReadOnly` proxy that swallows
mutators, which would silently turn an act into a no-op reported as done — and `jarvis doctor`
would then apply remedies, which is not what the user typed. The health sweep is the wrong home
for the opposite reason: it owns a model call and a thread pool, and no model call is this
feature's selling point.

### 5.2 What the pass does

Per reconcile tick, per project, gated on `os.rules.enabled` (default false — off means the pass
does not run at all, not "runs in dry run", and `jarvis rules list` says so):

1. Read the non-retracted detectors scoped to `''` or this project. One query, cached for the
   tick.
2. For each open order — terminal, hidden and `budget_exhausted` skipped — build `rules.facts(
   store, wo, *, now)` ONCE and evaluate every detector against it. Fifty rules cost one
   snapshot, not fifty.
3. **Build the snapshot LAZILY.** `holds.held` walks up to `holds._EVENT_LIMIT` events per order;
   read holds only for detectors whose condition mentions a hold field, and the same for every
   other non-trivial source. Prove it with a CALL-COUNT test that names `holds.held` explicitly —
   wrap it in a counter and assert ZERO calls when no live detector's condition names a hold
   field. That is a white-box assertion this repo does not otherwise make, and it is worth it
   here: naming the accessor is what makes a later refactor of it fail loudly instead of quietly
   reintroducing the walk. Separately, the pull request BODY states the measured wall clock of one
   `rules_tick` over N orders. That is a deliverable for a reviewer to read, not a test.
4. A detector that matches, and whose `health.fingerprint` for that order differs from the newest
   OPEN fire's, writes a `rule_fires` row with `mode="dry_run"`, `outcome="recorded"` and
   increments `hits`. **In `dry_run` nothing else happens: no alarm, no message, no flag, no
   gate request, no event on the order's timeline.** A dry run is invisible to the work order on
   purpose and appears only on §9's view.
5. A detector with an open fire whose condition no longer holds has that fire CLOSED, with
   `cleared_at`, `cleared_seconds` and `detectors.last_cleared`.
6. A detector that raises, or whose snapshot cannot be built, is caught PER DETECTOR, recorded
   `unreadable`, and does not stop the other detectors or the tick — the discipline
   `check_project` already applies to a broken invariant.

**`subjects` is `work_order` in v1.** Feature-order subjects are additive later:
`remedies.REMEDIES` already carries the subject vocabulary and `remedies._carrier_id` already
makes the feature-to-carrier hop, so nothing is being painted into a corner.

Cap the work explicitly: `EVAL_MAX_ORDERS` per project per tick, oldest first so nothing
starves, and a wall-clock budget after which the pass stops and records that it did.

### 5.3 Seeding the five rules, and proving each one fires

The seed DATA is §3.5's. This section owns the CALL SITE — seeding runs where `gate_rules`' does
and is idempotent by `seed_version` — and it owns the proof that the five conditions actually
select the situations they were written for, which §3.5 cannot check because nothing evaluates
there.

**Each seed rule ships with a POSITIVE and a NEGATIVE test.** The positive: it fires on a
reconstructed situation matching the issue it came from. The negative, and it is the one that
carries the weight: a HEALTHY order of the SAME status on which it must not fire, asserted by
`detector_id` and not by a total fire count — another rule's silence otherwise hides this one's
noise.

**If the historical orders are not in the dev databases, the situations are SYNTHESISED from
the store fixtures** — which is weaker evidence than a replay, and the pull request must say so
rather than let a reader assume a replay happened.

This section also owns the three assertions §3.5 cannot make: that every seed row's `primitive`
is in `remedies.REMEDIES`, that every parameter set satisfies that primitive's declared
`Remedy.params` schema, and that `rules.resolve` returns a `Resolution` for every one of the five
seed rules. §3.5 checks only that the five CONDITIONS parse, and §3.4 tests `resolve` against the
primitives that already ship, because the six primitives the seed rules name do not exist until
§4 lands and a section cannot assert against code that is not there yet. This is the only section
that depends on both §3 and §4, so it is the only one that can carry these. None of the three is
optional and none is §3's job. (Neo, question 878.)

## 6. Arming: a fire becomes an alarm and takes the gate that already exists

An ARMED detector's fire raises a `wo_alarms` row and then does nothing else. That is the whole
bridge, and it is why this feature adds no new review path, no new Neo question kind and no new
gate kind.

`ProjectStore.add_finding(wo_id, kind=…, reason=…, seq=NO_TURN, source="rule", probe=<detector
id>, remedy=<primitive>, remedy_argument=<the remedy row's argument>)`. Then
`Daemon.remedy_tick` picks it up exactly as it picks up a supervisor proposal,
`remedies.propose` files the `self_heal` approval and the `kind="approval"` Neo question,
`gates.apply_decision` routes the verdict to `remedies.record_verdict`, and `remedies.apply`
refuses unless the grant is live. **Not one line of that path changes.** The fire row records
`outcome="proposed"` with the `alarm_id`, or `outcome="refused"` with the refusal's words.

What this section adds beyond the raise:

- `project_store.ALARM_SOURCES` gains `"rule"` (it is `("cost", "health")` today) and the
  `source` assertion in `add_finding` then admits it.
- New timeline kinds `rule_fired`, `rule_dry_run`, `rule_cleared`, added to
  `project_store.ALARM_EVENT_KINDS` so `health.observer_kinds()` inherits the exclusion — §2's
  correctness rule, and the reason this cannot be a loose event kind written at the call site.
- `seq` is `project_store.NO_TURN` (`-1`). `add_finding` already defaults to it, so a rule firing
  on a `pending` order with no turn is honestly representable rather than a lie the alarm
  surfaces render. Verify it renders as absent on `/alarms` and in `jarvis alarms`, and fix the
  renderer rather than the value if it does not.
- `jarvis rules arm` flips the status and the change is audited on the row (`armed_by`,
  `armed_reason`, `armed_at`) and announced once in the inbox at `info` — the user must be able
  to see a rule gain acting authority, and must not be paged for it.
- **The supervisor still proposes on the same alarm shape** once a rule is armed for a gap.
  `remedies._refusal` already declines when a remedy for that alarm awaits a verdict, so the
  collision is contained — prove that with a test rather than assuming it.

**False positives are marked by a person, never derived.** `jarvis rules false-positive
<fire-id> --reason "…"` sets the flag and increments `false_positives` on both rows. The
interlock: an armed detector collecting `FALSE_POSITIVE_DISARM` (default 2) of them returns to
`dry_run` with a reason naming them, and an inbox row at `warning`. A person may arm a rule; a
person may also say it was wrong, and the second must be cheaper than the first.

`arm` and this bridge are ONE piece of work on purpose. "A rule may act" and "here is the
reviewed path by which it acts" are one review; an `arm` verb landing before the bridge exists is
a switch wired to nothing, and the next session to touch it will wire it to something quicker.

## 7. What a fix order carries, and the mechanical gate before the panel is paid

### 7.1 Three columns on `work_orders`, not `metadata`

```
gap_class      TEXT   -- the class of gap this order closes
detector_id    TEXT   -- a detectors.id, or a proposed detector's id
remedy_rule_id TEXT   -- a remedy_rules.id
```

Added to the `SCHEMA` string AND to `project_store.ADDED_COLUMNS`, because the table already
ships. Nullable, no backfill: every pre-existing row reads "not a fix order", which is true.

**Columns and not `metadata`, on this codebase's own stated rule** — a column when several
readers need the value without parsing a blob and when something must SELECT on it.
`work_orders.issue_url`'s comment is the precedent verbatim, arguing itself from
`Daemon.sync_issues` being a single indexed query that usually returns nothing. The readers here
are the pre-check, `evidence.collect_work_order`, §8's recurrence lookup, `jarvis wo show` and
§9's view. Five readers, one a query. And keep the improvement-order back-link where it already
is, in `metadata[ORIGIN_IO_KEY]` — do not add a fourth column for it.

**They are set structurally and never inferred from prose.** `jarvis wo create` gains
`--gap-class`, `--detector-id` and `--remedy-rule-id`; `ops.create_work_order` takes them as
keyword arguments; the re-scoped investigation order (wo-4beada49) stamps them when it files a
fix order. An empty `gap_class` is the ordinary case and skips every gate below. Nothing scans a
description, ever — kn-5d8a396a is the standing lesson about deriving a structural decision from
free text, and a `gap_class` guessed from prose puts the panel's payment behind a regex over a
worker's paragraph.

`jarvis wo show` and the work order's page render the three as links when they resolve, and say
plainly when a `detector_id` names no registered row. That is not an error: a fix order is filed
BEFORE its rule is registered, and "proposed, not yet registered" is the normal state of a fresh
one.

### 7.2 The pre-check

`rules.precheck(detectors, remedy_rules, wo, trigger_facts) -> str | None` — a PURE function
returning `None` to proceed or the refusal's precise words, the shape of
`evidence.nothing_to_judge` and `automerge.decide`. For a work order with a non-empty
`gap_class`:

1. `detectors_for_gap(gap_class, project)` returns a non-retracted row.
2. `remedy_rules_for(that row)` returns a non-retracted row whose `primitive` is in
   `remedies.REMEDIES`, and matching the order's `remedy_rule_id` if one is set.
3. The condition MATCHES the order the investigation was filed from — a real evaluation through
   §3.2's evaluator against that order's recorded facts, not a syntax check.
4. That remedy's `can_apply` against the same subject returns `""`.

**Called at the SUBMISSION SITE, `ops.submit_for_validation`, before the packet is collected and
before the round is opened** — the round is what costs money, so a refusal that opens one has
already failed at its job, and a paperwork failure must not burn a round number in a feature
whose whole subject is orders that burn rounds for nothing. Call it a second time in
`Daemon._validate_work_order`, in the band beside `evidence.nothing_to_judge` and
`_repeat_submission`, for rounds opened by routes that do not pass through `finish`; there it
refuses through `self._reject(...)` so the worker gets the reason, and that one does spend a
round, which the reason must say.

Any of the four failing refuses with the precise reason: the `gap_class` with no detector, the
primitive that does not exist, the condition that did not match and the leaf that made it false,
or the precondition's own sentence.

**It refuses, it does not create.** The pre-check never registers the rule for the worker. A fix
order that has not registered its rule has not finished its job, and quietly doing it would
remove the only forcing function this feature has.

**The weak link, named so nobody has to discover it.** Finding the triggering order means
`metadata[ORIGIN_IO_KEY]` → the improvement order's `--ref` evidence → a concrete order. If
those refs do not resolve to one in this project, there is nothing to dry-run against. Then the
check REFUSES — a detector that has never been shown to fire is unproven — and the reason tells
the submitter to name the triggering order explicitly. That is the one case where the refusal is
about the OS's records rather than the code, and it must read that way.

**But a read that FAILED is not a refusal.** If the trigger order is named and cannot be read —
deleted, another project — record UNREADABLE, state it in the packet, and let the submission
through to the panel. The pinned ruling applies here as much as to a model call.

### 7.3 The checklist, in the packet and never in the system prompt

`evidence.EvidencePacket` gains `gap_class: str = ""`, `detector_id: str = ""`,
`remedy_rule_id: str = ""` and `self_evolution: str = ""` (the rendered checklist), all populated
in `evidence.collect_work_order`, which already takes `wo`.
`validation.build_packet_prompt` renders it under a heading, copying the `spec_ref` /
`spec_section` pattern exactly.

**In the packet, never in the system prompt.** `validation.build_shared_prefix` and
`build_seat_prompt` are byte-stable per seat on purpose, for the prompt cache; a conditional
section there costs a cache write on every ordinary round in the fleet.

**The fingerprint, and read `evidence.fingerprint`'s docstring before you touch it.** That
docstring declares its exclusion list UNCHANGED by name, and spends four paragraphs on one
failure: anything mixed in unconditionally changes the hash of every round already stored and
silently disables `Daemon._preceding_round`'s repeat guard, which is the only thing that catches a
submitter who changed nothing. `head`, `summary` and `history` are each excluded for that reason.

So follow the ASSUMPTIONS precedent, which is the one thing that ever widened the formula and the
only shape that is safe: assumptions are mixed in ONLY when there are any, *"so every work order
that files none hashes exactly as it did before that existed."* Identically here —
`gap_class`, `detector_id` and `remedy_rule_id` join the hash ONLY when `gap_class` is non-empty.
An ordinary order, which is almost every order, must hash byte-identically to what it hashes
today, and a test must assert that against a pre-change value rather than merely against itself.

Those three ids go in because a submitter who registers a different detector without touching the
diff HAS produced new evidence, and the repeat guard would otherwise call round 2 identical to
round 1. The rendered `self_evolution` text stays OUT: it is prose the OS injected, not something
the submitter produced, and it is regenerated every round — the `history` row of that docstring's
table, arriving through a new door.

**`EvidencePacket` therefore carries all three ids**, not just `gap_class`, or the function cannot
hash what this section says it hashes.

The three questions, and they are about QUALITY:

- Does the condition match the real symptom, or a coincidence of this one order's state? Would
  it over-fire on a healthy order — name one if you can.
- Is the remedy safe and idempotent for this condition, and is it the right primitive?
- Is there a test that would fail if the condition stopped matching, and one that would fail if
  it started matching a healthy order?

**Seats judge quality, never existence** — say that in the injected text. Existence was decided
in §7.2, and a seat re-deciding it produces a blocker the submitter cannot act on. And the
pinned ruling still governs: block only on a blocker, and "the condition could also check X" is a
follow-up ticket.

The pre-check and the checklist are ONE piece of work. A refusal with no checklist leaves seats
judging existence, which this design forbids; a checklist with no refusal is advice.

## 8. The recurrence ledger: a rule that missed is not a new gap

When an investigation lands on a `gap_class` for which a detector ALREADY exists, something
specific happened and it is not "a new bug": either the condition missed the symptom, or it
matched and the remedy failed, or the rule was still in `dry_run` and never acted. Filing that as
a fresh issue loses the one fact that matters — that the OS already tried.

```sql
CREATE TABLE IF NOT EXISTS rule_recurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    gap_class TEXT NOT NULL,
    detector_id TEXT NOT NULL REFERENCES detectors(id),
    project TEXT NOT NULL, order_id TEXT NOT NULL,
    io_id TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL,               -- missed | remedy_failed | not_armed
    original_fix_wo_id TEXT NOT NULL DEFAULT '',
    original_issue_url TEXT NOT NULL DEFAULT '',
    filed_note TEXT NOT NULL DEFAULT '', -- what happened on the tracker, or why nothing did
    note TEXT NOT NULL DEFAULT ''
);
```

`rules.recurrence(detectors, gap_class, *, project="")` returns the existing detector or `None`,
and `ops.record_recurrence(...)` derives the verdict from the fire record rather than guessing
it:

- `not_armed` — the detector is `dry_run`. There is a fire on this order, or there would have
  been. The recurrence is evidence FOR arming it, and that is the finding.
- `missed` — the detector is `armed` and has no fire on this order. The condition did not match
  what actually happened. The finding is about the condition.
- `remedy_failed` — the detector is `armed`, has a fire on this order, and that fire's outcome is
  `proposed` or `refused` while the order is stuck anyway. The finding is about the remedy, or
  about the gate that refused it.

**What it does with that: it links, it never duplicates.** The recurrence row is written and
`detectors.recurrences` increments; the ORIGINAL issue is reopened if closed and commented on if
open, carrying the order, the verdict and a `regression` label; the new fix order carries the
original's `fix_wo_id` so the provenance is one thread and not two. Reuse `issues.py`'s existing
client for both — do not add a second `gh` wrapper — and take kn-5d8a396a's lesson: search the
tracker by the SUBJECT ID and the gap class, keep `--state all`, and never by a proposed title,
or a closed duplicate drops out of the answer and is re-filed for ever.

**When `gh` cannot be reached the recurrence row is still written** and the failure is recorded
in `filed_note`. The ledger is the OS's own record; the tracker is a mirror of it, and a network
failure must not lose the finding.

`rules.recurrence` is also the function the re-scoped investigation order (wo-4beada49) calls to
decide "reopen or link, never duplicate". This section owns the lookup, the verdict, the ledger
and the tracker act, and ships them callable and tested; that order only wires its filing path
into them. Do NOT edit `findings.py` or `ops.review_findings` here — that is its territory and a
second editor of those two is the one real merge conflict in this plan.

## 9. The Evolution view: what the project learned, and whether it is compounding

`ops.evolution_report(project: str | None = None, *, days: int = 90) -> dict` returns ONE plain
dict. The CLI and the dashboard both render it verbatim and neither computes a number of its own
— the standing convention, and the reason two surfaces in this codebase have disagreed before.
The `jarvis cost` / `_print_bill` pair is the pattern to copy.

**The headline is one number: mechanical share.** Of the orders that got stuck in the window, the
fraction resolved by a rule firing rather than by an investigation being dispatched. Numerator:
distinct orders with an `applied` fire that then cleared. Denominator: those plus distinct orders
an investigation was opened on. Both halves count ORDERS, not fires, so one rule firing twice on
one order does not inflate it. The report carries the SERIES, not just today's value — a single
percentage with nothing to compare it to is the number that gets misread.

**Absent is never zero, and this report is where that bites hardest.** A project with no armed
rules has NO mechanical share: the report says "no rule has been armed yet" and the view prints
that sentence. `0%` would be a false claim — it says the OS tried and failed. Same for a detector
with no fires: no hit rate, not a rate of zero. **And the page must render correctly against an
empty registry**, because `uilog.record_error` plus `INV-UI-HEALTHY` turn a 500 into an inbox
item, and an empty registry is what every install starts with.

Report keys:

- `timeline[]` — everything the project learned, newest first, each
  `{ts, kind, id, headline, links: {io, fix_wo, issue, pr}}`, folding detectors added / armed /
  retracted, knowledge entries from `central_store.knowledge` (retractions included), gate
  exemptions learned and retracted from `central_store.gate_rules`, and gap classes first seen.
  A missing link renders as absent, not as a broken one.
- `by_gap_class[]` — occurrences bucketed by week over the window, from `rule_fires`.
- `per_rule[]` — status, hits, false positives, recurrences, last fired, last cleared, median
  `cleared_seconds`.
- `recurrences[]` — from §8, with verdicts. The count of `missed` against `remedy_failed` is what
  says whether conditions or remedies are the weaker half.
- `stuck_resolution` — time to resolution per gap class before and after a rule was armed:
  median `cleared_seconds` while `dry_run` against median once `armed`. The dry-run period is
  exactly the control group, which is the second reason rules start there. **It needs history on
  both sides of an arming event and there is none yet**, so ship it as an honest key reporting
  insufficient history, with fewer than `MIN_SAMPLES` on either side saying so.

Surfaces: `jarvis rules summary [project] [--days n] [--json]`, a dashboard page at `/evolution`
for the fleet and `/evolution/<project>` for one, and a section on each project page linking to
it. Nearest templates to copy are `alarms.html` + `_alarm.html` (a fleet list with a per-item
page) and `knowledge.html`. Every POST route delegates to `ops`. No new charting dependency —
the dashboard's existing idiom, and the numbers come from the report.

## 10. Out of scope, and why

- **Arming off a threshold.** §3.3: the column is recorded, nothing acts on it. Automating the
  flip once there is hit history to calibrate against is a follow-up, and it is the OS granting
  itself acting authority on its own evidence, which deserves its own review.
- **`open_investigation` as a primitive.** §4: it needs an entry point on an unmerged branch.
  One entry once that lands.
- **Rewiring `jarvis wo fix` to resolve through this registry.** Its remedy seam is on an
  unmerged branch (wo-dbea82cf), so the WIRING is a backlog item gated on that merge — not a
  child that waits on a branch, which is how orders get stranded. What is NOT deferred is the
  handle: §3.4's `rules.resolve` ships here, in the shape that command calls, with
  `remedies.REMEDIES` as its source, tested in §3.4 against the primitives that already ship and
  in §5.3 against all five seed rules. Both briefs must say the same thing — `remedies.REMEDIES`
  is the ONE registry and `wo fix` SELECTS from it rather than extending it. (Neo, questions 860
  and 878.)
- **wo-9f00e3b5's fleet-health trigger.** Its mechanical "not progressing" condition IS a
  detector and should become a seed rule rather than being built twice. Nothing named that exists
  in the tree yet. Note the id collision waiting: `probes.DEFAULT_PROBES` already has
  `no-progress` as a PROMPT, in the same `wo_alarms.probe` namespace.
- **A rule that writes a rule.** The OS may be given a rule and may disarm one that misfires; it
  may not synthesise a condition or a remedy. That crosses from a reflex into authorship and
  nothing in the review chain is shaped for it.
- **Feature-order subjects.** `subjects='work_order'` in v1; widening is additive.
- **Rules in `jarvis search`.**
- **Replacing `invariants.py`.** Several invariants could be expressed as rules. Migrating one is
  a separate decision about what the OS GUARANTEES versus what it merely tries.
- **Retrofitting `gap_class` onto past orders.** Historical orders have none, which is an honest
  record of when the field started existing.
- **A live `gh` read inside a detector.** §3.2 forbids it. A rule needing a fact the OS does not
  record is a request for the OS to record it.

## Agent profile

You are a Jarvis OS engineer building one slice of the self-evolution feature. Other workers are
building the other slices in parallel and you will never see their sessions. Your section of
`docs/specs/2026-09-27-self-evolution.md` is your job; the rest of the spec is context you may
read and must not implement.

**What you must know about this codebase.**

It is a stdlib-only Python core — argparse, sqlite3, json — in `src/jarvis/`, about 50 modules.
Imports run strictly downward: leaves (`paths`, `db`, `catalog`, `claude_cli`, `timeline`,
`probes`, `remedies`, `holds`, `health`, `gate_rules`, `testing`) → stores (`central_store`,
`project_store`, `neo_store`) → adapters → `dispatch`/`ops` → `daemon`/`cli`/`ui`.
`src/jarvis/rules.py` is a LEAF: stdlib plus `db`, `catalog` and `remedies`, nothing above.
`cli.py` imports every jarvis module lazily, inside function bodies. Three SQLite databases:
`os.db` (central), `neo.db`, and one per project at `<project>/.jarvis/jarvis.db`. No module
calls `sqlite3.connect` directly; everything goes through `db.connect`. A column added to a table
that already ships must go in BOTH the `SCHEMA` string and `ADDED_COLUMNS`, or every live
database fails on read while new installs pass every test.

Serena is activated and the code map is committed. **Read `.serena/memories/codebase-map.md` and
`work-order-lifecycle.md` before you explore the tree**, and use `find_symbol` and
`find_referencing_symbols` rather than grepping for symbols. Rediscovering the architecture is
the most expensive thing you can do with your context and it is already written down.

**The conventions you must follow.**

Business logic lives in `ops.py` and returns plain dicts; the CLI and the dashboard consume that
dict verbatim and neither derives a number. `src/jarvis/gate_rules.py` plus the `gate_rules`
table in `central_store.py` is the model for everything here — a matcher in code, rows in the
central store, builtins from a `seed_rows()` function, learned rows as data, retraction instead
of deletion, `--reason` required. When this spec is silent, do what `gate_rules` does. A parser
raises with EVERY problem at once (`plans.parse_plan`, `findings.parse_report`); a gate returns a
reason string or `None` (`evidence.nothing_to_judge`, `automerge.decide`).

Comments in this tree explain *why*, at length, and cite the issue number or knowledge-base id
that forced the decision. Match that density — it is the house style, not decoration. Prose obeys
the house style otherwise: compressed, no filler, no invented abbreviations.

Tests live in `tests/`. Run the TARGETED tests for what you changed and cite CI for the suite:
`.github/workflows/ci.yml` already runs the whole thing on every push, a full local run takes
~21 minutes, and the prompt cache TTL is 5 minutes — so a local full run guarantees your whole
conversation is re-sent at the cache-write rate, and the panel distrusts the green it produces
anyway. `uv sync --extra dev` first in a fresh worktree. Use the fixtures in
`src/jarvis/testing.py` and the fake `claude` executable; nothing touches the real CLI. A new
column is untested until one test writes it and another reads a row that predates it.

You work in a git worktree, you open your own pull request against `main`, and you never commit
to `main`. Merging, releasing and restarting services are gated: ask with `jarvis gate request`
BEFORE you are blocked. An apostrophe inside a double-quoted argument makes the worktree git
guard refuse the whole command (kn-cf7ff768) — avoid them in `jarvis …` arguments.

**The traps in this area, and they have all bitten before.**

*The remedy registry is closed at runtime and that is not negotiable.* kn-6c252734:
`tuple(REMEDIES) == SHIPPED_REMEDIES` is asserted, the AST walk in `tests/test_remedies.py` pins
every acting call inside a handler keyed on the enclosing function's name, `catalog.RemedyConfig`
ships off with an empty allow-list, and a `self_heal` grant is consumed through
`gates.open_gate`. The DB grows RULES, never PRIMITIVES. A rule is a row that NAMES a primitive.
If your section makes a row able to introduce behaviour, you have built the wrong thing. The
exclusions in `remedies.py`'s docstring — no cancelling a turn, no `set_status`, no `wo done`, no
`fo resume`, no killing a process — are a boundary and nothing may reach them.

*A fingerprint must not move because it was looked at* (`health.observer_kinds()`, and its
docstring is an argument you should read). Any event kind you write about a unit while watching
it goes into `project_store.ALARM_EVENT_KINDS`, or the dedupe can never engage and the same
finding returns the instant the user puts the flag down.

*A detector may not duplicate an invariant.* `invariants.py` already detects mechanically on the
same tick and repairs what is unambiguous. Where it already detects a condition, key your
condition off the `invariant` timeline event rather than re-deriving the predicate, and let the
rule contribute the REMEDY. And `invariants.true_blockers` owns `attention_reason`: read it,
never write one it cannot re-derive.

*Never fabricate a default answer from a failure* (pinned). A detector that raises, a snapshot
that cannot be built, a `gh` call that fails, a trigger order that cannot be read: each records
UNREADABLE, leaves the subject alone, stays retryable. Not a refusal, not a false positive, not a
decision.

*Absent is never zero.* No fires is not a hit rate of zero. No armed rules is not a mechanical
share of 0%. A field the OS never recorded is absent, not false — and in a condition it makes
every operator but `absent` evaluate FALSE. Print the sentence, not the digit.

*A review blocks only on blockers* (pinned). And the mechanical gate decides EXISTENCE while the
seats decide QUALITY; collapsing the two produces a rejection nobody can act on.

*Dedupe against the newest record, not every past one* (commit `0c1e3f9`). A condition standing
for hours is one fire. An open fire is closed when the condition clears, and only then may the
rule fire on that order again.

*A structural decision is never derived from prose* (kn-5d8a396a). `gap_class`, `detector_id`
and `remedy_rule_id` are columns set by an argument. Nothing greps a description. And a tracker
duplicate check searches by SUBJECT ID with `--state all`, never by a proposed title.

*No network call and no subprocess inside the evaluation pass.* It runs on the daemon's tick
thread in the reconcile band. Every pull-request and CI fact is the one the OS already recorded
from its own poll. Read expensive sources LAZILY — `holds.held` walks up to `_EVENT_LIMIT`
events per order.

*Nothing conditional in a seat's system prompt.* `validation.build_shared_prefix` and
`build_seat_prompt` are byte-stable per seat for the prompt cache. Injected content goes in the
EvidencePacket.

*Do not reimplement the catch-up proof* (kn-907c9a61). Parentage from GitHub plus content hashed
locally with `--full-index --no-ext-diff --no-textconv`; never `patch-id`; keep the new-side
blob id for binary files. It exists; call it.

**What you must never do.**

Do not weaken any of the four refusals in `remedies.py`. Do not add an entry to
`invariants.INVARIANTS` for a rule, and do not make `jarvis doctor` apply anything. Do not put
the evaluation pass in the health sweep — it must cost no model call. Do not create a detector in
any status but `dry_run`, and do not make any code path arm one off a counter. Do not delete a
detector, remedy, fire or recurrence row. Do not edit `findings.py` or `ops.review_findings` —
wo-4beada49 owns those. Do not add a charting dependency, an ORM, a YAML parser, a regex operator
or an expression evaluator: the condition is JSON read by a closed table of typed comparisons,
and `eval` in any spelling is the security hole this design exists to avoid. Do not widen into a
sibling's section because it looked small; the seams were chosen deliberately.
