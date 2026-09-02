# The supervisor watches health, not just cost — and may be authorised to act

Feature order `fo-6269be9a`. Extends the supervisor shipped by feature order `fo-a10521d8`
(`docs/superpowers/specs/2026-08-31-the-supervisor.md`). **Read that spec first.**
Everything here is an amendment to it, and where the two disagree about the supervisor's
mandate, this one is later and wins.

## Why

The user, reviewing PR 173:

> I think there is a gap in the spec. The supervisor doesn't only care about cost or
> execution time of a work order or feature order, it cares about anything that is not
> working well, not in terms of functionality, but in terms of execution and ops. The
> whole design should have a configurable list of prompts of issues the supervisor should
> be aware of when assessing the health of the observed instrument (work order or feature
> order). The supervisor is an instrument that could trigger a self-healing action.

Three gaps.

**1. Ill-health is hard-wired to spend.** The only things that can raise an alarm are
`inspection.live_alarms`' three kinds — `long-turn`, `long-join`, `big-rewrite` — and all
three are arithmetic over a transcript. A work order parked in `waiting_input` for two days
behind a message nobody will send; one going round the same failing test for a fifth turn;
one whose worker has quietly stopped making progress: none of those costs an unusual number
of tokens, all of them are the OS not working, and the supervisor cannot see any of them.
What is needed is a list of symptoms **stated in words, in the catalog**, so that a new
class of "this is going badly" is a catalog edit rather than a release.

**2. The observed instrument is both a work order and a feature order.** Every alarm today
hangs off a `work_orders` row and `Daemon.check_burning_turns` walks only running work
orders. A feature order's planner and manager are themselves work orders, so their sessions
are covered — but the FEATURE as a unit is not. A feature whose children keep failing and
being re-filed, or which has been `executing` for four days, is unhealthy in a way no single
work order shows.

**3. The supervisor can see and cannot act.** `fo-a10521d8` stopped there deliberately and
pinned it in code. That was right for a judge whose only evidence was a cost heuristic. It
is the wrong resting place for a judge that can see a worker stuck behind a question nobody
will answer, because the remedy is one sentence and the alternative is the user's attention
— the exact thing the supervisor exists to spend less of.

## Two boundaries this feature must not blur

Both of these will be crossed by a well-meaning worker unless they are stated here.

**A computed symptom stays computed. Prose is ADDITIVE, never a replacement.**
`inspection.alarms` (`src/jarvis/inspection.py`, around line 703) is free, deterministic,
and fires the instant a threshold is crossed. A prose probe costs a model call and can only
fire on a heartbeat. The first instinct on reading this spec is to re-express `long-turn`,
`long-join` and `big-rewrite` as three prompts and delete the arithmetic; that is a
regression in cost, latency and reliability at once. **Two tiers, deliberately**: computed
symptoms keep their event-driven trigger and their module; prose symptoms arrive on a
sweep. They meet only at the alarm row, which is the point of §1.

**A symptom that can be stated as a deterministic post-condition belongs in
`invariants.py`, not in a probe.** "Self-healing" already exists in this OS and it is not
an agent: `invariants` re-evaluates post-conditions every reconcile tick and repairs what is
unambiguous, with no model call — `INV-FEATURE-FALSE-FAILURE` literally un-fails a feature
order whose children have recovered. If you can write the predicate in three lines of
Python, write it there. A probe is for what only judgement can see. Without this line, this
feature becomes the dumping ground for things that should have been invariants and the fleet
pays a model call every half hour for a predicate.

## Prerequisite, stated because it is not on `main`

§3 of the supervisor spec — the Neo escalation, `neo_store.Q_KINDS`' fourth entry `alarm`,
`supervisor.ALARM_REVIEWER_PERSONA`, `supervisor._escalate`, `Daemon._deliver_alarm_verdict`
and the answerability guard in `ops.neo_answer_escalated` + `ui/templates/_question.html` —
is **pull request 173, open and unmerged** (branch `worktree-wo-cbac07e3`). Nothing here
re-implements any of it, and **no section of this spec adds a Neo question kind**, which is
what keeps the conflict surface to `supervisor.py`'s and `daemon.py`'s import blocks rather
than to five shared lines. §4 and §5 both name what to do if `Q_KINDS` does not yet contain
`"alarm"` when their worker starts.

## Architecture at a glance

| Piece | Where |
|---|---|
| the finding | `wo_alarms`, widened: a SUBJECT (`work_order` or `feature_order`) and a SOURCE (`cost` or `health`) |
| the carrier | the work order a feature's row, events and attention hang off — `ProjectStore.carrier_for_feature` |
| what "unhealthy" means, in words | `probes.HealthProbe`, `os.supervisor.probes` in the catalog |
| when the OS looks | `health.fingerprint` + `health.due` — pure, model-free — and `health_reviews`, the ledger of looking |
| the sweep | `supervisor.review_health` + `Daemon.health_tick` |
| the judge | `supervisor.review` — **unchanged**. One review path for every alarm |
| the actions | `src/jarvis/remedies.py`, a CLOSED registry of two |
| the permission | an `approvals` row of gate kind `self_heal`, reviewed by Neo, decided with `jarvis gate approve` |
| its memory | `learnings` in `neo.db`, `seat="supervisor"` — unchanged |

**One row, one review path, one memory.** A probe finding and a cost alarm are the same kind
of object and differ only in what raised them; a feature order and a work order are two
subjects of the same row. Every place this feature was tempted to build a parallel mechanism
it widens the existing one instead, because two mechanisms that report the same thing are
how the two come to report different things.

**The supervisor decides; it never acts.** `supervisor.py` names no acting function — the
AST pin at `tests/test_supervisor.py::test_the_supervisor_never_names_a_command_that_acts_on_a_work_order`
stays byte-identical and must still pass when this feature is finished. A separate module
acts, and only under a gate grant.

## What must not regress, in any section

- **A failure must never become a judgement.** An unreadable review leaves the alarm
  unresolved with the attention flag up; an unreadable *sweep* raises nothing at all. §4
  explains why those polarities are opposite and consistent.
- **Nothing reaches a worker except a remedy with an approved, unspent grant.** The two
  locks §3 of the old spec installed — `Daemon._neo_drain`'s `deliver()` branch order and
  the refusal of a `kind='alarm'` question in `ops.neo_answer_escalated` and
  `_question.html` — stay exactly as they are.
- **Every model call goes through `agent_usage.record`**, and every distinct KIND of call
  gets its own `agent_usage.KIND_LABELS` entry, or `jarvis cost` prints a bare kind.
- **Every threshold lives in the catalog with per-project inheritance**, never as a module
  constant.
- **`ops.list_cost_alarms`' sixteen published keys keep their names and meanings.** Four
  keys are added and that is the whole licence (`kn-4d8449f1`); nothing else in this feature
  may add a seventeenth.

---

## 1 — The finding: one row, two subjects, two sources

**Goal: a `wo_alarms` row can name a feature order as its subject and a health probe as its
source, every alarm surface in the tree renders byte-identically for the rows that exist
today, and nothing yet raises a new one.** No model call, no probe, no remedy. This is the
foundation §4, §5 and §6 stand on, and it is the only section that touches the schema.

### The feature is the SUBJECT; a work order is the CARRIER

`wo_alarms.wo_id` is `TEXT NOT NULL REFERENCES work_orders(id)`. **It stays that way.**
SQLite cannot relax `NOT NULL` with `ALTER TABLE ADD COLUMN`; doing so needs a twelve-step
table rebuild, and `_migrate()` runs inside `ProjectStore.__init__` — every CLI invocation
and every reconcile of every project, over live production databases. That is the most
dangerous change this feature could contain and it buys nothing the carrier does not.

The carrier is already the house answer and is already documented. Read `ops.feature_event`:
*"A feature order has no timeline of its own — `wo_events.wo_id` is a real foreign key into
`work_orders` — so every step of its life is recorded on whichever work order carried that
step."* A finding about a feature is recorded the same way.

**But `feature_event`'s carrier rule is too narrow to reuse.** It resolves the carrier as
`manager_work_order(fo_id)` and returns `False` when there is none — and its own docstring
says a manager exists only when a plan was released with `os.validation.enabled` on,
*"which is every feature today"*. So this section owns the general rule, in one place:

```python
def carrier_for_feature(self, fo_id: str) -> dict | None:
    """manager -> feature_orders.plan_wo_id -> newest child -> None."""
```

`None` means the feature has no session at all — a `pending` feature that has not even been
planned — and there is nothing to observe. Make `feature_event`'s manager-only rule the
narrow case beside this one rather than a second, disagreeing rule.

### The columns, all through `ADDED_COLUMNS`

```python
"wo_alarms": {
    "subject_kind": "TEXT NOT NULL DEFAULT 'work_order'",   # work_order | feature_order
    "fo_id": "TEXT",                                        # set iff subject_kind is feature_order
    "source": "TEXT NOT NULL DEFAULT 'cost'",               # cost | health
    "probe": "TEXT",                                        # the probe id, for a health finding
}
```

No `CHECK`, no rebuild, no `NOT NULL` relaxation. `fo_id` carries no foreign key for the
same reason: adding one needs a rebuild. Enforce the pairing in Python — `subject_kind ==
'feature_order'` iff `fo_id` is set — and raise a `ValueError` naming both when it is
violated, because a constraint the database does not enforce and Python does not either is a
constraint that fails as a wrong page three weeks later.

**`seq` stays `NOT NULL` and a subject-level finding writes `-1`.** There is no turn; `-1`
is the sentinel and it is declared here as `NO_TURN`. `cli.cmd_alarms_show` prints
`turn {a['seq']}` unconditionally today — render `-1` as `no turn` on every surface that
prints it, in this section, or the first health finding prints `turn -1` to the user.

### The vocabularies, frozen here for §4 and §5

Declare all of these now, including the values later sections write. Two sections editing the
same tuple is a conflict for no reason; one section declaring them all is free.

```python
ALARM_SUBJECTS = ("work_order", "feature_order")
ALARM_SOURCES  = ("cost", "health")
ALARM_STATUSES = (... existing ..., "proposed")     # written by §5
ALARM_VERDICTS = ("ack", "escalate", "propose")     # "propose" written by §5
```

And the event kinds, with their payloads — §4 and §6 write and render them, and neither may
invent one:

| kind | payload | written by |
|---|---|---|
| `health_finding` | `{alarm_id, probe, subject_kind, subject_id, reason}` | §4 |
| `health_reviewed` | `{subject_kind, subject_id, trigger, findings}` | §4 |
| `remedy_proposed` | `{alarm_id, approval_id, remedy, argument}` | §5 |
| `remedy_applied` | `{alarm_id, approval_id, remedy, result}` | §5 |
| `remedy_refused` | `{alarm_id, approval_id, remedy, reason}` | §5 |

**That vocabulary is written down TWICE in this codebase and nothing checks the two agree.**
`project_store.ALARM_EVENT_KINDS` and `timeline.ALARM_KINDS` are hand-maintained lists of the
same thing in different modules. Grow both, and add
`assert timeline.ALARM_KINDS == frozenset(project_store.ALARM_EVENT_KINDS)` in this diff —
if only one grows, §6's `timeline._ref` silently stops resolving the new kinds and every
deep link on the page dies with no error anywhere.

### Store API

- `carrier_for_feature(fo_id)` as above.
- `add_finding(wo_id, *, kind, reason, seq=NO_TURN, source="cost", probe=None,
  subject_kind="work_order", fo_id=None) -> dict` beside the existing `add_alarm`, which
  keeps its signature and its one caller (`check_burning_turns`) untouched. Two entry points
  rather than one widened one, because `add_alarm`'s call site is inside the dedupe that §1
  of the old spec spent a whole section protecting and it must not be edited at all.
- `alarms_of(wo_id)` unchanged; add `alarms_for_feature(fo_id)`.
- `alarms_across(limit=200, statuses=None, wo_id=None, fo_id=None, sources=None)` — keeps
  its `JOIN work_orders` on the CARRIER, so every column it returns today keeps coming from
  where it comes from today, and adds `LEFT JOIN feature_orders f ON f.id = a.fo_id`.

**THAT JOIN IS UNCONDITIONAL. `fo_id` is a `WHERE` filter, not a join switch**, and reading
it the other way is the single easiest way to ship this section broken. `ops._find_alarm` —
the read behind `/alarms/{project}/{alarm_id}`, `ops.alarm_detail` and `jarvis alarms show`
— resolves an alarm with `store.alarms_across(wo_id=bare["wo_id"], limit=1000)` and never
passes `fo_id`. So does `ops.list_cost_alarms()` on the unfiltered path that feeds the page
and the badge. Join only when the filter was supplied and every one of those surfaces
renders the CARRIER's title and links `/wo/`. The assertion that catches it:
`ops.alarm_detail(<a feature finding's id>)["title"] == <the feature's title>`, with no
`fo_id` argument anywhere in the call.

`claim_next_alarm` and `reclaim_stale_alarms` key on `status` and are already
subject-agnostic. Do not touch them — and add one test that a feature-subject finding is
claimable, because that claim is the seam §4 depends on and nothing else proves it survived.

### The published dict gains exactly four keys and no more

`kn-4d8449f1` closed `ops.list_cost_alarms` at sixteen keys and four surfaces bind them
rather than validate them. The four this feature adds — and the licence stops here:

`source`, `probe`, `subject_kind`, `subject_id` (`fo_id or wo_id`, published so that no
surface has to branch to build a link).

Two of the existing sixteen change where they are *read from*, and this one substitution is
what makes every template render correctly with no template change:

- **`title` and `status` come from the SUBJECT** — the feature order's when there is one,
  the work order's otherwise.
- **`live` is unchanged and still derived from the CARRIER's `needs_attention`.** §1 of the
  old spec froze that and the freeze is still right: acking clears the ask and must not erase
  the record of what the fleet spent.

**No existing test can catch getting those two backwards, and none ever will unless you
build the fixture that diverges.** For every alarm on the tree today the subject IS the
carrier, so swapping the rules is a no-op on the whole suite —
`tests/test_inspection.py::test_the_alarm_status_is_not_the_thing_the_page_calls_live` grades
a different axis (`alarm_status` versus `live`) and passes either way. The fixture that
discriminates: one feature order titled `the feature` in status `executing`, whose carrier
work order is titled `the carrier` in a different status, with `needs_attention` set on the
**carrier only**. Then `title == "the feature"`, `status == "executing"`, `live is True` —
and after `ops.ack_attention(<carrier>)`, `live is False` with `title` unchanged.

### The attention flag goes on the CARRIER, and this is the trap that eats the section

**Do NOT raise a feature finding through `ProjectStore.flag_feature_attention`.**
`clear_feature_attention` is called *unconditionally* at eight sites across `ops.py`,
`daemon.py` and `invariants.py`, and `feature_orders` has one `attention_reason` string with
**no `acknowledged_blockers` analogue** — compare `work_orders`, where that column is the
entire reason an ack sticks. A health flag raised on a feature is wiped by the next
validation submission or plan release, silently, and the user's ack is never remembered.

Flag the CARRIER work order, through the machinery `ops.ack_attention` already governs, and
name the feature in the reason (`"fo-1a2b: …"`). The argument §2 of the old spec made for
`ack_attention` over `clear_attention` transfers verbatim.

### The badge is counting the wrong thing the moment a feature finding exists

`ui/app.py:alarm_badge` counts `len({a["wo_id"] for a in ops.list_cost_alarms() if a["live"]})`.
Two findings on two different features that share a carrier collapse into one, and two
findings on one feature reached through different carriers count as two. Count `subject_id`.
The docstring's reasoning — several alarms on one subject are one ask — is right and is
exactly what the wrong key breaks. `alarms_page`'s grouping (`live.setdefault(a["wo_id"], …)`)
has the identical fix.

**Both halves or the test grades nothing.** "Two subjects sharing one carrier count as two"
is satisfied by deleting the `set()` and counting rows. The other half — two findings on ONE
feature reached through TWO carriers count as **one** — is what proves it is still a set over
subjects. Assert them in the same test, for the badge and for the page's ack-form count.

### The reads

`jarvis alarms --fo <fo-id>` beside `--wo`, and `--source cost|health`. Both are reads §6
and a future PR body need, so they exist here. `/alarms/{project}/{alarm_id}` for a
feature-subject finding links the subject at `/fo/{project}/{fo_id}` — the route
`ui/app.py` already serves — and not at `/wo/`.

### What must not change

The four surface assertions in `tests/test_inspection.py` still pass verbatim:
`action="/wo/proj_a/{wo_id}/ack"`, `name="back" value="alarms"`,
`alarms<span class="nav-badge">1</span>`, and the `"nothing is burning"` /
`"1 asking for you"` strings from `cli.main(["alarms"])`. `Daemon.check_burning_turns` keeps
its `(kind, seq)` event dedupe byte-for-byte; move it and every cost alarm re-raises on
every reconcile tick for the life of the turn.

### Done

`add_finding` with `subject_kind='feature_order'` and no `fo_id` (and the reverse) raises
`ValueError` naming both fields; `carrier_for_feature` resolves manager, then `plan_wo_id`,
then newest child, then `None`, with a test for each rung; `claim_next_alarm` returns a
feature finding **and the claimed dict carries `subject_kind`, `fo_id`, `source`, `probe` and
`seq == NO_TURN`** (the claim's `WHERE` is on `status` alone, so "it was claimable" grades
nothing on its own); `ops.list_cost_alarms` returns twenty keys, with `subject_id` equal to
the feature's id for a finding **and** to the work order's id for a legacy cost alarm in the
same test; the divergent fixture above proves `title`/`status` come from the subject while
`live` comes from the carrier; `ops.alarm_detail(<al-id>)["title"]` is the feature's title
with no `fo_id` argument passed; `alarm_badge` returns 2 for two findings on two features
sharing one carrier **and** 1 for two findings on one feature reached through two carriers;
`jarvis alarms --fo` and `--source health` filter; `/alarms/<project>/<al-id>` for a feature
finding returns 200 with `href="/fo/` and no `href="/wo/` inside that alarm's own block;
`jarvis alarms show` prints `no turn` and **not** `turn -1` for a finding while still printing
`turn 1` for a cost alarm in the same test; `timeline.ALARM_KINDS` equals
`frozenset(project_store.ALARM_EVENT_KINDS)`; the four `tests/test_inspection.py` literals
still pass verbatim and unedited; and a legacy `wo_alarms` row read through
`ops.list_cost_alarms` after three store opens comes back with `subject_kind == "work_order"`,
`source == "cost"`, `subject_id == wo_id` and `probe is None` — the defaults landing where a
surface binds them, which is the real risk, not the column count.

---

## 2 — The symptom catalogue: health probes as configuration

**Goal: what counts as "going badly" is a list of prompts in the catalog, resolved per
project probe-by-probe, visible on a CLI read, and rendered into a checklist the supervisor
can be handed.** This section adds NO trigger and raises NO finding: it ships a list that §4
reads. Say that in the pull request, because a reviewer expecting behaviour will hunt for it.

### `src/jarvis/probes.py`, a leaf

```python
@dataclass(frozen=True)
class HealthProbe:
    id: str                                   # kebab-case, unique — and the alarm's `probe`
    title: str                                # what a surface calls it, in three words
    prompt: str                               # the symptom, addressed to the supervisor
    subjects: tuple[str, ...] = ("work_order", "feature_order")
    enabled: bool = True

DEFAULT_PROBES: tuple[HealthProbe, ...] = (...)
def resolve(base, override) -> tuple[HealthProbe, ...]   # merge by id — see below
def armed(probes, subject_kind) -> tuple[HealthProbe, ...]
def render_checklist(armed) -> str                       # what §4 puts in a system prompt
```

A leaf with no `jarvis` imports, so `catalog` can import it the way it already imports
`neo_store`. The prose lives here rather than in `catalog.py` for the same reason
`gates.REVIEWER_PERSONA` and `plans.PLAN_REVIEWER_PERSONA` live in the modules that own
their kinds.

`id` becomes `wo_alarms.probe` and is constrained: lowercase `[a-z0-9-]`, unique within a
resolved list, and it may **not collide with `inspection`'s three alarm kinds**
(`long-turn`, `long-join`, `big-rewrite`) — a probe shadowing one would make two different
things read as one on every surface. Refuse the collision at catalog-parse time, naming the
offending id.

### The five shipped probes, and they are content rather than scaffolding

A probe list nobody wrote is a feature nobody can use, so these are part of the deliverable.
Write each `prompt` as a paragraph in the second person describing the symptom, **what in
the evidence packet would show it, and what would innocently look like it**. A one-line
prompt produces a detector that fires on everything.

| id | subjects | the symptom, in substance |
|---|---|---|
| `no-progress` | both | Nothing has changed on the record for a long time: no new turn, no message, no event. The instrument is nominally open and is not moving. |
| `going-in-circles` | work_order | The same tool, file, test or error recurs across turns without the state changing. Effort is being re-spent rather than advancing. |
| `waiting-on-nobody` | both | Blocked on something nobody is going to resolve: a queued message nothing consumes, a decision already made, a dependency that can never clear. |
| `failing-children` | feature_order | Children failing, being superseded or re-filed repeatedly. The feature is not converging. |
| `brief-mismatch` | both | What the instrument is doing has drifted from what it was asked to do. |

Each prompt must also say what the supervisor should do about a symptom it can see but
cannot explain: report it, do not embellish it. The finding's `reason` is what the user
reads on `/alarms`.

### Per-project inheritance is MERGE BY ID, and that is a decision

`_parse_inspect` and `_parse_validation` do field-level inheritance: a project naming one key
keeps the OS answer for the rest. A list has no fields, so the rule is restated one level
down. **A project's `probes` entries merge over the OS list by `id`:**

- an entry whose `id` matches an OS probe **replaces that probe's named fields** — so
  `{"id": "no-progress", "enabled": false}` switches one off for one project and
  `{"id": "no-progress", "prompt": "…"}` rewords it;
- an entry with a new `id` **adds** a probe;
- an OS probe the project does not name is **inherited unchanged**.

Wholesale replacement was rejected: it makes "watch for one more thing here" mean
re-declaring the fleet list, which then silently stops tracking the fleet's later edits — the
failure `kn-6ca2bcd9` describes for validation config, one level up. A project cannot DELETE
an inherited probe, only disable it, so the record of what the fleet watches for stays
legible.

### TWO SHARED FUNCTIONS BREAK LOUDLY ON THE NEW FIELDS — AND THE DODGE IS THE REAL DANGER

Both failures below are **loud**: they take out most of the suite the moment you add the
fields. That is the good news and it is why they are not the risk. **The risk is the
workaround**, because both have a tempting one that leaves a green suite and a broken
feature. Read to the end of this block before you fix either.

**`catalog._parse_supervisor` parses every field that is not `enabled` or `model`
reflectively as an `int`:**

```python
numbers = {k: v for k, v in vars(base).items() if k not in ("enabled", "model")}
cfg = SupervisorConfig(..., **{k: int(raw.get(k, v)) for k, v in numbers.items()})
```

`_parse_supervisor` is called unconditionally from `parse_catalog`, so **every**
`parse_catalog` in the suite goes through that line. Two of the fields this section adds trip
it, in two different ways, and fixing one is not fixing the other:

- `probes: tuple[HealthProbe, ...]` makes it `int((HealthProbe(...),))` — a `TypeError` on
  every catalog load, including `jarvis start`.
- **`health_enabled: bool` does NOT raise there.** `int(False)` is `0`, and then the
  `value < 1` loop just below raises `CatalogError: os.supervisor.health_enabled must be
  >= 1` on every catalog load instead. A worker who widens the exclusion set for `probes`
  alone still bricks the OS, and the traceback points somewhere else.

Widen the exclusion for both; the reflective loop is deliberate — a numeric field added later
reaches the catalog with no edit here — so widen the exclusion, do not unroll the loop.

**And then check you still READ them.** Excluding a field from `numbers` removes it from the
constructor call, so `health_enabled` is silently pinned at its default however the catalog
sets it, and every "ships off" assertion in §4 passes on a switch that can never be turned
on. Nothing in the tree catches that. The pin, in the shape of `_parse_inspect`'s existing
inheritance test: an OS block with `health_enabled: true` and a project overriding it to
`false`, asserting `cat.os.supervisor.health_enabled is True`,
`<project>.supervisor.health_enabled is False`, **and**
`config_version.resolve(cat)["projects.<p>.supervisor.health_enabled"] is False`.

**`config_version._jsonable` has no dataclass branch.** It handles `Path`, `set`, `tuple`,
`list` and `dict` and returns anything else untouched, so a `tuple[HealthProbe, ...]` reaches
`canonicalise` → `json.dumps` → `TypeError`, breaking `jarvis config show`, `jarvis config
set`, and every config-version write on every command. `_flatten` right above it already
recurses into dataclasses with `is_dataclass(value)`, so the branch to add is the same
predicate:

```python
if is_dataclass(value) and not isinstance(value, type):
    return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
```

`tests/test_config_version.py::test_a_resolved_map_survives_a_json_round_trip` already does
`json.loads(json.dumps(resolve(parse_catalog(...))))` over a default `os.supervisor`, so a
non-empty `DEFAULT_PROBES` takes it out, along with every `ops.config_set` test in
`tests/test_config_console.py`. `_coerce` (the way back) is only reached by
`validation_config_from_resolved` and needs no change; say so in a comment so the next reader
does not go looking.

**THE DODGE, AND THE TEST THAT ALREADY GUARDS IT.** The cheap way out is to add `probes` to
`config_version._SKIP` so it never reaches `_jsonable` at all — which hides every probe from
`jarvis config show`, from the config console and from the config version stamp, so nothing
records which probe list judged a unit. `tests/test_supervisor.py::test_every_supervisor_setting_reaches_the_config_console`
iterates `vars(catalog.SupervisorConfig())` and demands each name be a resolved key, so it
goes red on that dodge. **It may not be weakened, skipped or narrowed** — teach `_jsonable`
about dataclasses instead. All eight fields this section adds must pass it.

### `SupervisorConfig` also gains §4's numbers

Create them here so §4 touches no catalog code and the two can be reviewed apart:

```python
probes: tuple[HealthProbe, ...] = probes_mod.DEFAULT_PROBES
health_enabled: bool = False        # §4's switch
health_every_ticks: int = 20
health_min_interval_minutes: int = 30
health_stale_minutes: int = 720
health_max_units_per_tick: int = 4
max_enabled_probes: int = 12
probe_prompt_chars: int = 800
```

`SAFETY_KEYS` gains `os.supervisor.health_enabled`, beside the `os.supervisor.enabled`
already there and for the same reason: it removes a watcher, and the change is invisible on
every surface until the thing it was watching goes wrong.

Refusals in `_parse_supervisor`: a probe id colliding with an `inspection` kind, a duplicate
id in one list, an unknown `subjects` entry, a prompt over `probe_prompt_chars`, more
enabled probes than `max_enabled_probes` (they all ride in one prompt). Each names the
offender.

### `supervisor.build_system_prompt` gains an additive keyword

`build_system_prompt(store, project, learnings_limit=None, probes=())` — with `probes=()`
its output must be **byte-identical to today's**, and that is this section's most important
pin. The cost review and the health sweep share this function and the cost prompt's prefix
stability is load-bearing: a changed prefix reprices every review silently.

**The existing byte-stability test does not grade this.**
`tests/test_supervisor.py::test_the_system_prompt_is_byte_stable_across_reviews` compares two
calls in one process; it catches a clock in the prompt, not a changed template, so a worker
who unconditionally appends an empty checklist header passes it. Commit the current output as
a literal in the test file and assert equality for `probes=()`; then, in the same test, assert
that the two-probe call **starts with** that literal and is longer. Prefix-extension is the
property the cache actually needs, and equality alone would forbid the feature.

### The CLI read: `jarvis supervisor probes [project]`

What is this project actually watched for, and where did each answer come from — `fleet`,
`project override` or `project addition`. `kn-42c52cec`'s lesson: a resolved value the user
cannot see is a value they cannot trust, and probe inheritance is exactly the kind of
resolution that goes wrong quietly. `--json` prints the resolved list.

`jarvis config set` addresses scalars by dotted path and `_flatten` puts the whole tuple at
`os.supervisor.probes` as one value, so a single probe is not editable from the console.
That is acceptable — a catalog file edit is the intended route and is what "a catalog edit
rather than a release" means — but say it in the pull request and file a backlog item rather
than inventing a second editing path here.

### The test that will pass for the wrong reason

`assert len(cfg.probes) == 5` is green off the defaults with no parser at all. What grades
the inheritance is the **three cases in one test**: an OS block with three probes and a
project block naming one of them with `enabled: false` plus one new id — then assert the
project's resolved list is four long, that the disabled one is *present and disabled* rather
than absent, that the untouched one is byte-identical to the OS entry, and that the OS-level
resolved list is still three. Asserting only the length passes on wholesale replacement.

### Done

`probes.resolve` implements the three merge cases in the one four-assertion test above,
including that the OS-level list is **unchanged** afterwards (which is what catches a merge
that mutates the shared `DEFAULT_PROBES` tuple in place); each of the five refusals raises
`CatalogError` naming the offender; `parse_catalog` and `config_version.resolve` both succeed
over a catalog **that declares a probe**, round-tripping through `canonicalise`;
`test_every_supervisor_setting_reaches_the_config_console` and
`test_a_resolved_map_survives_a_json_round_trip` pass **unmodified**; `health_enabled`
inherits per project and reaches the resolved map as `False` when a project says so;
`build_system_prompt(..., probes=())` equals a literal committed in the test, and the
two-probe call starts with it and is longer; `render_checklist` over two probes satisfies
`out.index(p1) < out.index(p2)`; `jarvis supervisor probes` on a project that disables one
fleet probe and adds one of its own shows the disabled probe **present and marked disabled**
with source `project override`, the new one as `project addition` and an untouched one as
`fleet`; `os.supervisor.health_enabled` is in `SAFETY_KEYS`.

"Nothing in this diff raises a finding or makes a model call" is a property of the diff and no
test can grade it — the closest observable is that with two probes configured one
`daemon.tick()` makes zero `claude` calls and writes zero alarm rows. Assert that, and say
"this section adds no trigger" in the pull request body; the real check is the reviewer's.

---

## 3 — The evidence packet for a feature order

**Goal: `supervisor.build_evidence` can describe a FEATURE order as well as a work order,
under the same budget, with the work-order packet byte-identical to today.** A pure function
over the project store. No daemon, no model call, no schema, no catalog.

### What exists now

`supervisor.build_evidence(pstore, wo, alarm, cfg=None, inspect_cfg=None)` composes four
sections — the alarm, the work order, the session turn by turn (via
`inspection.read_session`, the same read `live_alarms` makes), and the last
`cfg.quoted_turns` of `timeline.build_conversation` — and clips them **whole sections at a
time** to `cfg.evidence_budget_chars`, appending a stated omission. Read it before you touch
it; the clipping discipline and the comment explaining it are the parts to preserve.

### The signature

```python
def build_evidence(pstore, subject: dict[str, Any], alarm: dict[str, Any],
                   cfg=None, inspect_cfg=None) -> str
```

`subject` is `{"kind": "work_order" | "feature_order", "row": <the store row>}`. Update the
existing call site in `supervisor.review` and nowhere else — §4 is the only other caller and
it does not exist yet.

**The work-order packet must be byte-identical to what today's function returns for the same
inputs.** A packet that changes shape invalidates the cached prompt prefix and quietly
reprices every review that ever runs.

"Capture it before, refactor, assert equality" is a **procedure, not a criterion** — it leaves
no artefact in the finished tree, because after the refactor there is nothing left to compare
against. **Commit the expected packet as a literal in the test file** (or a fixture under
`tests/data/`), with `jarvis.db.now` patched to a fixed float — `build_evidence`'s first
section renders `{minutes:.0f} minute(s) ago` off the real clock — and a fixed two-turn
transcript. That assertion survives the commit that creates it, which is the whole point.
There is no existing byte-stability test for `build_evidence`; the only one in this family is
for `build_system_prompt` and it compares two calls in one process, so it grades
nondeterminism rather than a changed template.

**Cross-section seam, and both halves are needed or the judge is shown nonsense.** §1's
`NO_TURN` sentinel is `-1`, and that first section renders `raised on turn {alarm['seq']}`
unconditionally. A health finding would reach the supervisor's own prompt reading `raised on
turn -1`. Render it as `raised on no particular turn` when `seq` is `NO_TURN`, here — §1 fixes
the same sentinel on the CLI surfaces and cannot reach this string.

### What a feature order's packet contains

A feature has no session and no transcript, so `_session_lines` stays work-order-only and
the feature branch reads what the feature page already reads. Named here, because a spec
that leaves this to the worker gets a second implementation of the feature page:

- **the feature** — id, title, status, how long it has held that status, `needs_attention`
  and its reason;
- **the child tree** — from `pstore.feature_children(fo_id)`: each child's id, title,
  status, `spec_section`, `depends_on`, whether it carries a `pr_url`. This is the whole
  substance of `failing-children` and without it that probe cannot fire;
- **what has already been answered for** — `SUPERSEDED_CHILDREN_KEY` in the feature's
  metadata, the record of failures the user has ruled on with `jarvis fo resume`. A
  supervisor that does not read it re-reports a decision the user has already made, which is
  the single most annoying thing this feature could do;
- **validation** — `pstore.validation_rounds(fo_id=…)`: each round's number, outcome, and
  the reason the submitter was sent back;
- **what was last said** — the CARRIER's last `cfg.quoted_turns` conversation turns, through
  the same `timeline.build_conversation` the work-order branch uses. Use
  `carrier_for_feature`; a feature whose carrier is `None` gets a packet saying so rather
  than an exception.

### The budget is the trap here, not the content

A twelve-child feature blows `evidence_budget_chars` on the child tree alone. Keep the
whole-section clipping and add a within-section bound for the tree: `failed`, `blocked` and
oldest-unfinished children first, then a counted omission line (`"…and 7 further children,
all completed"`). A judge that cannot see it was shown a fraction weighs the fraction as the
whole — the comment already in the function. Pin it the way `worker_brief.CORE_BUDGET_CHARS`
is pinned: build a feature with twelve children and a long plan and assert the packet is
under the budget.

### Done

`build_evidence` over a work-order subject equals a literal committed in the test, character
for character, with `jarvis.db.now` patched; over a feature subject it names the feature's
status, every failed child by id, the superseded-children note and the last validation round's
outcome, **and does not contain `"# The work order"`** (which is what catches a fall-through
into the wrong branch); a twelve-child feature's packet is under the budget, is over 1000
characters (an empty stub is under the budget too), ends with the exact counted omission
`and 7 further children`, and the three `failed` children's ids are the ones that survived the
clip while a `completed` one did not; a feature whose carrier is `None` produces a packet
containing `"# The feature"` and the exact no-carrier sentence, rather than `""` or an
exception; and with `jarvis.inspection.read_session` monkeypatched to a counter, it is called
**exactly once** for a work-order subject and **exactly zero** times for a feature subject, in
one test. A health finding's packet says `raised on no particular turn` and never `turn -1`.
`tests/test_supervisor.py::test_the_evidence_packet_is_capped_and_says_so` and
`::test_the_packet_carries_the_alarm_the_order_and_what_the_worker_last_said` still pass with
only the `subject` argument reshaped.

---

## 4 — The health sweep: what makes the OS look, and when

**Goal: on a bounded cadence, an open instrument whose state has changed — or which has not
changed for too long — is judged against the project's armed probes, and each symptom found
becomes an ordinary alarm on the right subject.** Everything downstream of that alarm is
machinery that already exists.

### The problem, stated plainly

Today the trigger IS the threshold: `check_burning_turns` computes a number, compares it to a
catalog value, raises. A symptom stated in words has no number to cross, so nothing notices
the moment it becomes true. The answer is a **sweep** — and the sweep's hard part is not the
prompt, it is deciding when to spend a model call.

### The trigger: a model-free fingerprint, plus a staleness clause

Keep two jobs strictly apart. *The prompts decide what is wrong; a pure function decides when
to look.*

```python
# src/jarvis/health.py — a leaf. No model call, no transcript read.
def fingerprint(pstore, subject) -> str
def due(review_row, fingerprint, cfg, now) -> str | None   # 'first-look'|'changed'|'stale'|None
```

The fingerprint for a work order: `(status, latest_turn.seq, latest_turn.state, event
count, needs_attention, pending assumption count, queued message count)`. For a feature:
`(status, the tuple of child statuses, latest validation round number, superseded-children
count)`. Cheap, deterministic, and it never opens a transcript.

- **`changed`** — the fingerprint differs from the last recorded one, and
  `health_min_interval_minutes` has passed. A unit nothing has happened to is not looked at
  twice.
- **`stale`** — the fingerprint has NOT moved for `health_stale_minutes`, and that unit gets
  exactly one review until it moves again. **This clause is what earns the feature.**
  "Nothing has changed for three days" is the most valuable health signal this OS has and a
  pure delta trigger would never fire on it.
- **`first-look`** — never reviewed, and older than `health_min_interval_minutes`.

`due` returns the trigger name or `None`, and the name is recorded on the review row and put
in the packet, so "why did it look now" is answerable without a model.

### The ledger: `health_reviews`, and it is NOT `wo_alarms`

```sql
CREATE TABLE IF NOT EXISTS health_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL, trigger TEXT NOT NULL,
  outcome TEXT NOT NULL,              -- clear | findings | failed
  findings INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_health_subject ON health_reviews(subject_kind, subject_id, ts);
```

A plain `CREATE TABLE IF NOT EXISTS` in `ProjectStore.SCHEMA`, no migration, no foreign key
(the subject may be either kind and neither can carry a cascade here) — and therefore
`ProjectStore.delete_work_order` and `delete_feature_order` must delete its rows explicitly.
`delete_work_order` already deletes six child tables and returns a counts dict that
`tests/test_wo_hide_delete.py::test_delete_work_order_cascades` asserts with `==` over
exactly those six keys, so **do not add a seventh key**; delete the rows and add a positive
assertion after the delete instead.

**The looking is recorded separately from the finding, and that is a decision.** Writing "we
looked and found nothing" into `wo_alarms` would fill `alarms_across`, `list_cost_alarms`,
`/alarms`' "On the record" half and `jarvis wo show`'s `alarms:` line with noise, and
filtering it out at every surface is exactly the permanent union read §1 of the old spec
argued against. The ledger is also the dedupe memory — see below.

### `supervisor.review_health`

```python
def review_health(pstore, neo_store, project, subject, probes, cfg,
                  trigger: str, record=None, inspect_cfg=None) -> dict
```

**ONE MODEL CALL PER UNIT, NOT ONE PER PROBE.** The system prompt is
`build_system_prompt(neo_store, project, cfg.learnings_limit, probes=armed)` — §2's additive
keyword, so the persona, the learnings block and the checklist share one byte-stable prefix
per project. The user prompt is §3's evidence packet plus the trigger. N probes × M units ×
every half hour as separate calls is a fleet-wide bill nobody has measured; a checklist
inside one call is the same information for one call.

Output contract:

```json
{"findings": [{"probe": "<probe id>", "reason": "<one line: what is wrong, for the user>",
               "evidence": "<what in the packet says so>"}]}
```

`findings` **absent raises** — it is what says this is a sweep reply at all, the same call
`supervisor._validate` makes about `decision`. `findings: []` is the healthy answer, is the
common case, must write no alarm, and writes one `health_reviews` row at `outcome='clear'`.
A `probe` id not in the armed list is dropped with a logged warning rather than raising: a
hallucinated id is one bad finding, not a bad reply.

### The fail-safe polarity is INVERTED here, and it is the subtlest thing in the feature

`supervisor.review`'s fallback **escalates**, because output nobody can read must not become
an ack. `review_health`'s fallback **finds nothing**, because output nobody can read must not
become an attention item the user cannot trace to anything. Both obey one rule — *a failure
never invents a judgement* — and they land on opposite sides of it because the judgements
point in opposite directions. Put that in a comment; the next reader will otherwise "fix" one
to match the other.

A failed sweep writes `health_reviews.outcome='failed'` with the reason and **does not record
the fingerprint as reviewed**, so the next tick retries rather than treating a failure as a
look. Both failure routes, and `structured.request`'s `on_invalid` covers only one:
`claude_cli.ClaudeCliError` propagates untouched by design (`kn-9b18a8eb`), so the transport
case needs the caller's own `try/except` or the sweep raises out of the daemon's thread pool
— `supervisor.review`'s `_transport_failure` is the shape to copy. Three tests, each
asserting zero alarms and one `failed` row: (a) non-JSON, (b) `ClaudeCliError`, (c) valid
JSON with `findings` absent.

`agent_usage.record(kind="health", …)` — **a separate kind from `supervisor`**, with its own
`KIND_LABELS` entry ("supervisor health review"). The sweep is the standing cost of watching
and the review is the per-alarm cost of judging; folded together, `jarvis cost` cannot answer
"what does watching cost", which is the first question anyone asks before turning this on.

### YOU MUST TEACH THE FAKE `claude` TO ANSWER A SWEEP, AND IT IS THIS SECTION'S FIRST TASK

Nothing else in this section can be graded until you do. `src/jarvis/testing.py` recognises a
supervisor call by the **first line of `SUPERVISOR_PERSONA`** and answers every one of them
with `{"decision": "ack", …}` — which has no `findings` key. A health sweep shares that
persona, so out of the box the fake's reply fails `review_health`'s validator, the fail-safe
fires, and every sweep records `outcome='failed'` and raises nothing. **Every "one tick leaves
exactly one alarm row" assertion then fails, and — far worse — every "zero alarms were raised"
assertion passes for entirely the wrong reason.**

Add a health arm inside the existing supervisor branch, keyed on something only a sweep prompt
carries (the checklist header), returning one finding by default, with `FORCE_HEALTH_CLEAR`,
`FORCE_HEALTH_GARBAGE` and `FORCE_HEALTH_FAIL` tokens for the empty case and the failure
shapes — the token idiom the fake already uses elsewhere. **Do not change
`SUPERVISOR_PERSONA`'s first line**: `testing.py` and `tests/test_supervisor.py`'s
`_supervisor_calls` helper both match on it, and changing it makes every
`_supervisor_calls(...) == []` assertion in the repo vacuously true.

For the same reason, `_supervisor_calls` will count your sweeps as cost reviews. Write a
`_health_calls` helper keyed on the checklist, or this section's call-count assertions — which
are most of them — mean nothing.

Last: `tests/test_supervisor.py::test_nothing_in_the_module_hard_codes_a_threshold` AST-walks
**the whole of `supervisor.py`** and forbids numeric literals outside a tiny allow-list.
`review_health` lives there, so any `[:12]`, `[:800]` or `range(3)` you write trips it. Every
number comes from `cfg`.

### A finding becomes an ordinary alarm, and then nothing else happens here

`pstore.add_finding(carrier_wo_id, kind=<probe id>, reason=<the finding's reason>,
seq=NO_TURN, source="health", probe=<probe id>, subject_kind=…, fo_id=…)` at the default
`status='raised'`, a `health_finding` event on the carrier, and the attention flag on the
carrier if it is not already up — the three moves `check_burning_turns` makes.

**And then it stops.** The existing `_drain_project_alarms` claims it, `supervisor.review`
judges it, §3 of the old spec escalates it, §5 of the old spec renders it, §6 of the old spec
learns from it. Two model calls per finding is the price of one review path, one surface and
one memory, and it is the right price: the sweep answers *is something wrong*, the review
answers *does the user need to know*, and the whole design rests on being able to ask those
separately.

`supervisor.review` is **not modified by this section.** If `neo_store.Q_KINDS` does not yet
contain `"alarm"` when you start (PR 173 unmerged), that is fine and changes nothing here —
the escalate path degrades to recording `status='escalated'` with the flag up, which is what
§2 of the old spec already ships and is an honest state. Do not add a question kind.

### THE DEDUPE IS THE TRAP, AND IT IS WORSE HERE THAN FOR A COST ALARM

`check_burning_turns` dedupes on `(kind, seq)` because a cost alarm is about one turn. **A
health finding has no turn.** "This order has made no progress for two days" is true on every
sweep for as long as it is true, so with no dedupe the attention flag returns the instant the
user puts it down — the wallpaper failure §6.3 of the PR 159 spec exists to prevent, arriving
through a door that spec never had.

**Key the dedupe off `health_reviews.fingerprint`, never off events and never off alarm
status.** Do not raise a finding for `(subject, probe)` when a finding for that pair was
already raised at the same fingerprint. Status must not enter the predicate: keying on "no
OPEN alarm for this probe" re-raises the moment the supervisor acks one, which is every time
it works correctly. A changed fingerprint means the state genuinely moved and a still-true
symptom is worth re-stating; an unchanged one means nothing has happened.

A single-sweep test cannot see this, and **the obvious two-tick test passes with a completely
broken dedupe** — because `health_min_interval_minutes` defaults to 30 and both ticks happen
inside one test second, so the second tick may never reach a model call at all, and "one
alarm, two review rows" is then false in the other direction or true because nothing happened.

Four assertions, in one test, are what grade it:

1. two sweeps GENUINELY RAN — `len(_health_calls(fake)) == 2`;
2. two `health_reviews` rows carrying the **same** `fingerprint`, and exactly one `wo_alarms`
   row;
3. between ticks two and three, force the finding to `status='acked'` and clear the flag: a
   third tick at the unchanged fingerprint must still produce **no** new alarm. That is the
   "status must not enter the predicate" clause and nothing else tests it;
4. then mutate the unit, tick again, and assert a review row with a **different** fingerprint
   and a second alarm.

Patch `jarvis.db.now` while CREATING rows, inside `with monkeypatch.context() as m:` — a bare
`monkeypatch.undo()` reverts the `jarvis_home` and `catalog_file` fixtures and every surface
then renders as an empty OS with no error anywhere. That patching is also the only way to
exercise the `stale` trigger at all, since its clause is measured in hours.

### `Daemon.health_tick`

Modelled on `supervisor_tick`, and **its own pool and its own drain guard.**
`supervisor_tick` is event-driven off a threshold and answers a burning turn; a fleet sweep
queued in front of it would delay the thing the whole mechanism was built for. Not the tick
thread either — a model call inside the per-project loop stalls every project in the fleet —
and not Neo's, where the seats already serialise the question FIFO.

- gated on `project.supervisor.enabled and project.supervisor.health_enabled`, and on
  `health_every_ticks`;
- candidates: work orders in `project_store.OPEN_STATUSES` whose `origin` is not in
  `daemon.UNGOVERNED_ORIGINS`, plus feature orders in `FO_OPEN_STATUSES` that have a carrier.
  `waiting_input` is in `OPEN_STATUSES` and belongs here on purpose: an order parked behind a
  message nobody will send is exactly what `waiting-on-nobody` exists to catch, and it is
  invisible to every cost heuristic;
- `health_max_units_per_tick` bounds the spend — a fleet with forty open units must not fire
  forty calls in one tick. **Take the longest-unreviewed first** so the cap rotates rather
  than starving the tail.

### It ships off

`health_enabled = False`, on top of `enabled = False`. Pin it as BEHAVIOUR and **assert on
call counts**: with the catalog untouched, zero `claude` invocations, zero `agent_calls` rows
of kind `health`, zero `health_reviews` rows, zero `wo_alarms` rows with `source='health'`,
and `jarvis alarms` rendering exactly as §1 left it.

**That pin is unfalsifiable on its own** — it is green on an empty diff and green on a
working one. It needs a sibling test over the SAME fixture with both switches on, asserting
all four counts are non-zero. Only the pair says anything.

### Done

With both switches on, three probes armed and the fake returning one finding, one tick makes
**exactly one** model call carrying the checklist (which is the only assertion proving one call
per unit rather than one per probe) and leaves exactly one `wo_alarms` row with
`source='health'`, `probe` equal to the probe's id, `seq == NO_TURN` and the reason as given;
one `health_finding` event; one `health_reviews` row at `outcome='findings'`; the carrier
flagged with a reason naming the feature; and one `agent_calls` row of kind `health` with
`KIND_LABELS["health"]` present and not equal to `"health"`. The four-assertion dedupe test
above holds. `{"findings": []}` leaves zero alarms and one `clear` row. Each of the three
failure shapes leaves zero alarms and one `failed` row — and a **second tick at the same
fingerprint makes a second call and writes a second row**, which is how "the fingerprint was
not recorded as reviewed" becomes observable rather than asserted about an implementation
detail. A `completed` feature order is not swept while an `executing` one with a carrier is,
in one test with a total call count of exactly 1. `health_max_units_per_tick` is respected
with a fifth candidate present, **and that fifth unit is swept on the next tick** — the cap
rotates, longest-unreviewed first, rather than starving the tail. Deleting a work order
removes its `health_reviews` rows while another subject's survive, and
`tests/test_wo_hide_delete.py::test_delete_work_order_cascades`' six-key `counts` dict is
unchanged. With the catalog untouched, all four counts are zero, and the sibling test with
both switches on has all four non-zero.

---

## 5 — The remedy: a closed vocabulary, a gate, and the daemon that applies it

**Goal: the supervisor can propose one of an enumerated set of non-destructive actions; the
existing gate reviews it; and the daemon — never the agent — applies it once permission is
granted.** This is the section that changes what the OS is allowed to do, and it is written
to be reviewable on its own for exactly that reason.

**Propose and apply are ONE work order and must not be split.** A reviewer of "the OS may
now act on a work order" has to see, in one diff, both what may be proposed and the code that
runs it; split, the reviewer of the first half has no way to judge blast radius.

### How far the rule bends

The old spec's §2 forbids the supervisor from acting and pins it with
`tests/test_supervisor.py::test_the_supervisor_never_names_a_command_that_acts_on_a_work_order`,
an AST walk over `supervisor.py` for the names `cancel`, `cancel_work_order`, `set_status`,
`send_message` and `queue_message`. **That test stays byte-identical and must still pass.**
`supervisor.py` names an id from an enumeration and nothing else — it may not hold the
remedy's prose, its target or its handler, and note the pin trips on a local variable or a
field called `cancel`, so keep the vocabulary entirely in `remedies.py`.

What changes is that a different module, under a different authority, may act. The permission
is bounded by four independent things, any one of which refuses:

1. a **closed registry** in code — a remedy not in it does not exist;
2. a **per-project allow-list** in the catalog, shipped empty;
3. a **mandatory gate grant** — approved, unexpired, unspent;
4. a **single acting module**, with an AST pin keeping the acting calls inside the handlers.

### `src/jarvis/remedies.py`

```python
@dataclass(frozen=True)
class Remedy:
    id: str
    headline: str          # what it does, in the terms a reviewer needs
    blast: str             # what it touches, and what it cannot undo
    subjects: tuple[str, ...]
    apply: Callable        # apply(pstore, central, project, subject, row) -> str

REMEDIES: dict[str, Remedy] = {...}
SHIPPED_REMEDIES: tuple[str, ...] = ("nudge", "unblock")
```

**Exactly two, both non-destructive.** Two rather than one, because two is what proves the
registry is a registry:

| id | subjects | what it does |
|---|---|---|
| `nudge` | both | Queue ONE short message asking the session to say where it is — on the work order, or on a feature's `carrier_for_feature(fo_id)`. |
| `unblock` | work_order | `ops.unblock_work_order` in its **default, dead-edges-only** mode — cut a dependency edge that can never clear because the dependency was cancelled, failed or deleted. Never `drop_all`. |

### THE NUDGE DOES NOT GO THROUGH `ops.send_message`, AND THIS IS NOT A STYLE CHOICE

`ops.send_message` ends with `if wo["needs_attention"]: store.clear_attention(wo_id)` — a
correct move for a *user's reply*, which IS the answer to whatever flagged them. But
`ProjectStore.clear_attention` sets `acknowledged_blockers = NULL`, so routing the nudge
through it **wipes the exact column this section's own Done list requires to survive**, before
`remedies.apply` ever reaches `ops.ack_attention`.

Follow `ops.nudge_pr_conflict` instead, which is the precedent for an OS-authored message and
does exactly the right two things: `store.queue_message(wo_id, text, source="supervisor")`
followed by `store.add_event(...)`, and nothing else. Attention is `remedies.apply`'s business
and it goes through `ops.ack_attention`.

**`source="supervisor"` is a literal contract with §6.** `timeline._message_label` returns
`"you → worker"` for every source that is not `"neo"` and not in `UNAUTHORED_SOURCES`, so
`source="jarvis"` would render the supervisor's nudge in the conversation **as the user
speaking**. §6 adds the `_message_label` branch that turns this source into
`"supervisor → worker"`. Write the literal exactly; neither section can see the other's text.

The test that catches the `clear_attention` regression is one nobody writes unprompted: give
the work order a blocker `invariants.true_blockers` genuinely re-derives (`status="failed"`
plus `flag_attention`), ack it so `acknowledged_blockers` is populated, run the remedy, and
assert `json.loads(wo["acknowledged_blockers"])` still holds that blocker. The idiom is
already written in `tests/test_supervisor.py`'s ack tests.

**Say the nudge's cost out loud in the proposal the user reads.** Delivering a message
re-sends the worker's whole conversation at the cache-write rate, which is the very cost the
`big-rewrite` alarm exists to report. A remedy that spends money to ask a question is
sometimes right and must never be silent about it.

`remedies.get(id)` raises `KeyError` for anything else, and a test asserts
`tuple(REMEDIES) == SHIPPED_REMEDIES` — so adding a remedy is a test-breaking, reviewed act
rather than a prompt edit. There is no free-text action and no "other".

**Explicitly excluded, and this is the boundary:** cancelling a turn, `set_status`,
`wo done`, `fo resume`, killing a process. Each destroys work with no other record and each
needs the proposal loop to have earned trust first.

### Config

`catalog.RemedyConfig`, mounted at `os.supervisor.remedies`, field-level per-project
inheritance like every other block:

```python
enabled: bool = False
allowed: tuple[str, ...] = ()        # remedy ids; ships EMPTY
```

Two switches rather than one, deliberately: **`enabled: true` with `allowed: []` means the
supervisor may propose and nothing is ever applied**, which is a genuinely useful shipping
state and the one the fleet should run first. An unknown id in `allowed` is a `CatalogError`
naming the known ids — `GateConfig.parse`'s rule for unknown gate names, and for the same
reason: a typo must not silently leave a permission unset.

`SAFETY_KEYS` gains `*.supervisor.remedies.*` — `*.` rather than `os.`, because the
per-project form is the same switch with a smaller blast radius, exactly as `*.validation.*`
is written today. **Mind the ordering trap**: a dataclass used via
`field(default_factory=X)` must be defined ABOVE its user in `catalog.py`, or it is a
`NameError` at import. And `_parse_supervisor`'s reflective `int()` loop must exclude this
field too — §2 widened that exclusion set; add to it, do not re-derive it.

### The gate: the existing one, with its worker plumbing switched off

The user's ask points at `jarvis gate`, and reusing it buys Neo's review, the escalation
path, `jarvis gate list|show|approve|deny`, `/gates` and the audit trail — with **no new Neo
question kind**, because a heal request rides the existing `kind="approval"` question and
`Daemon._deliver_gate_verdict` looks its subject up in `approvals`, which is where this row
lives. That is the whole reason to reuse it.

A sixth `GateKind` in `gate_rules.KINDS`:

```python
GateKind(name="self_heal",
         summary="let the supervisor act on a work order or feature order it judged "
                 "unhealthy (this reaches a running session)",
         conflict_markers=())
```

Two things about it are unlike every other kind, and both belong in the code as comments:

- **It has no recogniser patterns and `gates.classify` never returns it.** Every other kind
  exists to catch a command a worker typed; this one is filed programmatically by the OS.
  Pin it: `classify` returns `self_heal` for nothing, including every string in
  `gate_rules`' own example corpus.
- **It does not ride `GateConfig.enabled`.** The command gates are opt-in per project
  because gating them trades one bottleneck for a slower one. This gate is *mandatory* — it
  is the only thing between a health judgement and a running worker — so
  `remedies.propose` never consults `GateConfig` and a project cannot switch it off. What a
  project controls is `remedies.allowed`, which is the other direction.

### `remedies.propose`, and the three things it must NOT inherit from `gates.file_request`

```python
def propose(pstore, neo, project, subject, alarm, remedy_id, argument, cfg) -> dict
```

It writes the `approvals` row through `ProjectStore.add_approval` and files the Neo question
through `neo.ask(..., kind="approval")` itself. It does **not** call `gates.file_request`,
for three reasons each of which is a defect if inherited:

- **`file_request` moves a `running`/`dispatching` work order to `waiting_input`.** A worker
  that asked for a gate has nothing to do until it is answered; here the worker did not ask
  and is very likely mid-turn. Parking it is read as "worker is waiting on your input" by
  `jarvis status`, the dashboard and `invariants.true_blockers` — the exact forty-minute
  defect the long comment at the end of `gates.apply_decision` was written about.
- **`approvals.command` would hold a lie.** Nothing will be executed by a shell. Put the
  rendered intent there (`heal al-1a2b: nudge wo-3c4d — "<argument>"`) and say in the code
  that it is not a command, so `gates.approved_message`'s "run this command again" wording
  never reaches anyone.
- The question's context must be the **alarm's evidence packet plus the supervisor's
  reasoning and the remedy's `headline` and `blast` in words**, so Neo rules on the remedy
  and not merely on the symptom.

Before writing anything, refuse: `remedies.enabled` false; the id not in `allowed`; the
remedy's `subjects` not covering this subject; an alarm that already has a proposal out.
**A refusal writes the reason on the alarm and files no approval** — the user must never be
asked to approve something their own catalog forbids.

### `gates.apply_decision` messages the worker on EVERY verdict, and that is the landmine

Its last moves are `store.queue_message(approval["wo_id"], message, source="gate")` and
`end_wait_if_nothing_is_out`. For a `self_heal` approval that message is addressed to a
worker that never made a request: on a denial it is noise delivered into a running turn,
which is precisely the act this whole feature is fenced against, and on an approval it is
redundant because the remedy itself is the intervention.

**Extend the shared function with one guard rather than forking it.** Two decision paths for
one table is how the two come to leave different state behind, and the repo's own convention
is to extend. A `self_heal` approval: queues no message, skips `end_wait_if_nothing_is_out`
(it never set a wait), and writes the alarm's outcome plus an inbox row instead. Everything
else — `decide_approval`, the `gate_decided` / `gate_dismissed` event, dismissal learning —
is unchanged and inherited. Pin both sides in one test: a `self_heal` approval leaves
`queued_messages(wo_id) == []` **and** a `pr_merge` approval still queues exactly one.

### `Daemon.remedy_tick`, and `remedies.apply`

```python
def apply(pstore, central, project, approval, alarm, subject) -> str
```

**Refuses unless `approval["status"] == "approved"` and `gates.open_gate` yields a live,
unspent grant.** It consumes the grant through `open_gate` — never by hand; that function is
also what closes the requests a grant makes moot — dispatches to the remedy's `apply`
callable, writes a `remedy_applied` event with the returned result string and an inbox row
naming what was done, and moves the alarm to `acked` through `ops.ack_attention` (**never
`ProjectStore.clear_attention`**, which wipes `acknowledged_blockers` and discards the user's
own earlier dismissals). Anything else raises `RemedyRefused` and performs nothing.

`Daemon.remedy_tick` sits beside `supervisor_tick`, shares its pool, and picks up alarms at
`status='proposed'` whose approval has since been approved. On a denial or dismissal the
alarm goes back to `escalated` with the reason, a `remedy_refused` event, and the attention
flag **up**: the user refused the remedy and the symptom it was for has not gone anywhere.

A proposal whose approval never resolves must not sit for ever. Add a check to `invariants`
in the shape of `check_neo_escalations_are_live`: an alarm at `proposed` whose approval is
closed, superseded or gone goes to `escalated` with a reason. Read `_stale_alarm_question`
first and copy its **"a missing subject is left alone"** decision — these checks run per
project against state that is not all per-project, and closing on absence is how one project
answers another's live request.

### The supervisor's third verdict

`supervisor._validate` accepts `{ack, escalate, propose}`. The contract:

```json
{"decision": "propose", "remedy": "<a remedy id>", "argument": "<what to say, or why>",
 "reason": "<why, 1-2 sentences, for the record>", "note": "<what the user is told, <=200 chars>"}
```

Three refusals, and none is a downgrade:

- `propose` with no `remedy`, or naming an id not in `REMEDIES`, is a **bad shape**: the
  verdict fails, the alarm goes to `failed`, the flag stays up. Never silently `escalate` —
  a supervisor that asked for an action it cannot name did not mean to escalate, and
  recording that it did puts a judgement nobody made on the record.
  **Careful: that bullet is already true on the tree you start from.** `_validate` rejects
  any decision outside `{ack, escalate}` today, so a fake replying
  `{"decision": "propose", "remedy": "reboot"}` already yields `failed` with no approval and
  the flag up — the assertion passes on an empty diff. It only grades once the same test also
  drives a *valid* `propose` and asserts `status == "proposed"`, `verdict == "propose"`, one
  `self_heal` approval, one Neo question, and the work order's status untouched.
- `propose` naming a remedy the project does not allow, or whose `subjects` exclude this
  subject, is refused by `propose` and the alarm goes to `escalated` with the refusal in
  `verdict_reason` — the user is the right next reader, because the supervisor believes an
  action is needed and is not permitted to take it.
- The persona must say all of this, and must list the armed remedies with their `blast`
  lines. A model told it may act, and not told what happens when it asks wrongly, asks
  wrongly. Extend `SUPERVISOR_PERSONA`; if `ALARM_REVIEWER_PERSONA` exists in your tree
  (PR 173), extend it too — Neo now rules on remedies and a reviewer told only about spend
  escalates every one of them. If it does not exist, do not create it.
  **`SUPERVISOR_PERSONA`'s FIRST LINE may not change.** Both `src/jarvis/testing.py`'s fake
  and `tests/test_supervisor.py`'s `_supervisor_calls` helper match on that literal to
  recognise a supervisor call; change it and every `_supervisor_calls(...) == []` assertion in
  the repository becomes vacuously true, fleet-wide, with nothing going red. Append, never
  rewrite the opening.

Two more constraints on this section's code, both enforced by tests already in the tree:
`tests/test_supervisor.py::test_nothing_in_the_module_hard_codes_a_threshold` AST-walks all of
`supervisor.py` and forbids new numeric literals, so the note's length bound comes from
`cfg.note_chars`; and adding `self_heal` to `gate_rules.KIND_NAMES` means several fixtures
that splat `list(gates.KIND_NAMES)` into `gates.enabled` will now enable it. That is harmless
— nothing classifies to it — and `GateConfig.parse(True).enabled == frozenset(KIND_NAMES)`
keeps passing. Say so in the pull request so nobody "fixes" it.

`supervisor._apply`'s new branch calls `remedies.propose` and nothing else, records
`status='proposed'` with `verdict='propose'` and the remedy on the row, and emits
`remedy_proposed`. **The attention flag stays up** while a proposal is out: the OS wants to
do something to the user's work and has not been told it may.

### The old spec is now wrong in the present tense, and you must fix it here

`docs/superpowers/specs/2026-08-31-the-supervisor.md`'s "Out of scope" opens with *"The
supervisor acting on the work order … The verdict vocabulary is `{ack, escalate}` and section
2 pins it in code."* That is read as current and is now false. **In this same pull request**,
rewrite that bullet in the past tense and point it at this spec — a spec that describes the
old design in the present tense is read as current, which is the whole of `kn-88615da4`. Do
not delete it: the reasoning in it is still why the gate exists.

### Every negative assertion needs a positive partner in the same test

`assert no message was queued` is green on a path that never ran.

1. **The registry is closed.** `remedies.get("restart-the-daemon")` raises;
   `remedies.get("nudge")` returns the row; `tuple(REMEDIES) == SHIPPED_REMEDIES`.
2. **The grant is mandatory.** `apply` with a `pending`, `denied`, `dismissed` and `expired`
   approval each raises `RemedyRefused` **with the acting function patched and its call count
   asserted zero** — then the same fixture with an approved grant calls it exactly once, and
   `get_approval(id)["uses"] == 1`, which is what proves the grant was SPENT through
   `gates.open_gate` rather than read around.
3. **The allow-list bites before the gate.** With `remedies.enabled` false, and separately
   with `allowed=()`, `propose` refuses, `pstore.pending_approvals(wo_id) == []` **and** the
   alarm carries the refusal reason; with `enabled` true and `allowed=("nudge",)` the same
   call files exactly one `self_heal` approval and one Neo question, and the work order's
   status is **unchanged**.
4. **The acting calls stay inside the handlers.** Reachability is not decidable from an AST,
   so state the pin in the form that is: for every `ast.Attribute.attr` / `ast.Name.id` in
   `{send_message, queue_message, unblock_work_order, cancel, cancel_work_order, set_status}`
   found anywhere in `remedies.py`, **the nearest enclosing `FunctionDef`'s name must be in
   `{r.apply.__name__ for r in REMEDIES.values()}`**. Do not copy
   `tests/test_neo_panel.py::test_neo_never_imports_the_panel` — it walks `ast.Import` only,
   and `remedies.py` legitimately imports `ops`, so an import walk grades nothing. Re-run the
   supervisor's own pin unchanged.
5. **The end-to-end pair, in one test.** On a work order that already carries a re-derivable
   blocker the user has acked, drive a real `propose` verdict: assert the approval EXISTS, the
   alarm is `proposed`, the flag is up and `queued_messages(wo_id) == []`. Then approve the
   grant, run one `remedy_tick`, and assert the message is NOW there with
   `source == "supervisor"`, the alarm is `acked`, the flag is down with the earlier
   `acknowledged_blockers` **still on the row**, and a `remedy_applied` event carries the
   result string.
6. **The stale-proposal invariant needs its untouched partner.** In one test: one alarm whose
   approval has vanished goes to `escalated` with a reason, and one alarm whose approval is
   still `pending` is compared row-to-row with `==` before and after and is unchanged.

### Done

`SHIPPED_REMEDIES` holds exactly two ids; `parse_catalog` refuses `allowed: ["reboot"]`;
`gates.classify` returns `self_heal` for no string in `gate_rules`' corpus; `propose` with
remedies off files nothing and records why; with them on it files one approval, one question,
and leaves the work order's status untouched; `apply_decision` on a `self_heal` approval
queues no worker message while a `pr_merge` approval still queues one; `apply` raises for
each non-approved status with the acting function uncalled and delivers exactly once under a
grant with `uses == 1`; a `propose` naming an unknown remedy leaves `failed` with no approval
**in the same test as a valid one that leaves `proposed`**; a denied proposal leaves
`escalated` with the flag up; the invariant closes a proposal whose approval vanished and
leaves a pending one byte-identical; the end-to-end pair holds with `acknowledged_blockers`
surviving and `source == "supervisor"` on the delivered message; both AST pins pass, the
supervisor's is byte-identical, `SUPERVISOR_PERSONA`'s first line is unchanged, and no numeric
literal was added to `supervisor.py`; the old spec's out-of-scope bullet names this one.

---

## 6 — The record, the surfaces and the memory

**Goal: everything this feature can produce — a health finding, a feature-order subject, a
proposed remedy and its outcome — is legible on the instrument's own record and on the alarm
surfaces, and a correction teaches the next review.** Buildable in parallel with §4: every
column is fillable directly with `ProjectStore.update_alarm` and every event with
`add_event`, so **seed the fixtures that way — including the screenshots.** Turning the
supervisor on to produce a picture burns a real model call.

### The timeline, and the branch that looks fine when it is missing

`timeline.event_level` returns `"signal"` for a kind it does not know, so an unhandled kind
renders as a bare kind name plus a JSON blob **and looks fine on the page**. The comment in
`timeline.py` says this has been learned here once already, on the validation kinds. Every
one of §1's five new kinds gets a `_describe` branch returning a `(label, detail)` pair, and
`_ref` resolves each to its alarm the way `cost_alarm` already does.

`assert event_level("remedy_applied") == "signal"` grades nothing — it passes before the
branch exists. Copy
`tests/test_timeline.py::test_the_four_validation_events_read_as_four_different_things`:
build all five, assert `len(set(labels)) == 5`, assert `label != entry["kind"]` for each,
assert one specific `detail` string, and assert `event_level("message_delivered") == "debug"`
in the same test so the classifier is proved to still discriminate at all.

**`build_conversation` gets the NUDGE's text and nothing else.** A remedy's message is
speech, addressed to a session; a proposal, a gate verdict and a finding are events. That is
the split §4 of the old spec made between a verdict and a note, applied again.

**Almost all of that is already true, and the one part that is not is the whole job.**
`build_conversation` already takes every `wo_messages` row, and its event loop already admits
only three kinds — so a `remedy_proposed` event is excluded for free and the nudge is included
for free. "Exactly one turn" is green before you start. What is NOT true is the attribution:
`timeline._message_label` returns `"you → worker"` for any source that is not `"neo"` and not
in `UNAUTHORED_SOURCES`, so §5's nudge currently renders **as the user speaking**. Add the
branch for `source == "supervisor"` → `"supervisor → worker"`. `UNAUTHORED_SOURCES` is the
wrong home for it: an unauthored message is one nobody decided, and the supervisor decided
this one.

The assertion that grades, in the tuple-equality idiom of
`test_the_conversation_carries_the_question_the_worker_asked` — which is also the only shape
that catches an event leaking in:

```python
[(c["kind"], c["who"], c["content"]) for c in convo] \
    == [("message", "supervisor → worker", "<the nudge text>")]
```

### The reads

- **`/alarms` keeps its three halves and gains no fourth.** A proposal is still "asking for
  you" until it is decided; an applied remedy is "addressed by the supervisor" like an ack.
  Inventing a fourth half splits one queue into two the user must remember to read.
- Each row gains what raised it — the probe's **`title`**, not its raw id (the id is a
  database value, not user copy; a cost alarm keeps its current rendering) — the subject and
  a link to it (`/wo/…` or `/fo/…`), and, when there is one, the remedy, its gate request
  linked to `/gates`, and what happened. The per-alarm page carries the same in full plus the
  `evidence` the finding cited.
- **`jarvis alarms` and `jarvis alarms show`** carry the same facts: the CLI is the OS, and a
  feature that exists only on a web page is a bug.
- **`jarvis fo show`** gains the alarm line in exactly `jarvis wo show`'s shape and
  vocabulary, extended with §5's `proposed` — `alarms: 2 (1 acked by the supervisor,
  1 awaiting your permission to act) — al-1a2b, al-3c4d`. `--json` carries the `wo_alarms`
  rows in full, as `jarvis wo show --json` does.
- **`jarvis wo show`**'s existing alarm line gains `proposed` and renders a health finding by
  probe title.

### The memory has to name the probe and the remedy

`supervisor.learning_from_review` renders *"On a `<kind>` alarm (`<reason>`) the supervisor
decided `<verdict>` because `<its reason>`. The user's ruling: `<feedback>`."* For a health
finding, `<kind>` is now a probe id rather than a cost kind, and for a `propose` verdict the
remedy is the entire content of the correction — "you were right that it was stuck, wrong to
nudge it" and "you should have nudged it" render identically today.

Widen the template to name the probe when the source is `health` and the remedy and its
argument when the verdict is `propose`, and **leave the cost/ack/escalate wording
byte-identical otherwise**.

**Do NOT pin that with the prefix property.** "Two learnings, assert the first prompt is a
prefix of the second" is already asserted by
`tests/test_supervisor_memory.py::test_the_supervisors_block_is_append_only`, it is green
today, and it is *structurally incapable* of detecting a reworded template — it is a property
of `neo.render_learnings`' ordering, and `learning_from_review` runs at write time so stored
rows are never re-rendered. The existing coverage is no better: it asserts two substrings are
present, so a worker who rewrites the sentence wholesale passes.

What grades it is **literal equality on the cost case**, in one test alongside the two new
shapes:

```python
assert supervisor.learning_from_review(
    {"kind": "long-turn", "reason": R, "verdict": "ack", "verdict_reason": W}, F) == (
    f"On a long-turn alarm ({R}) the supervisor decided ack because {W}. "
    f"The user's ruling: {F}")
```

plus a health finding whose sentence names the probe, plus a `propose` verdict whose sentence
names the remedy AND its argument — and all three must differ from each other.

`ops.review_alarm` refuses an alarm that is not `acked` or `escalated`, so a `proposed` one is
**already refused today** and asserting only that grades nothing. The work here is the
MESSAGE: it must name the status and say the way to answer a proposal is
`jarvis gate approve|deny`, because a user told "this alarm cannot be reviewed" and not told
where to go is a user who files a bug. Assert `pytest.raises(ops.OpsError, match="jarvis gate")`
together with `review_status == "unreviewed"` **and** `reviewed_at is None` — that last pair
is what proves the refusal happens before the first write rather than eventually.

### The tests that will pass for the wrong reason

- **Jinja renders an absent key as empty**, so `assert remedy in page` is trivially true when
  the remedy is `""`. Build fixtures with distinctive strings, assert those exact strings, and
  add one case where a distinctive string is deliberately absent from the context and assert
  it is not on the page.
- `assert client.get("/alarms").status_code == 200` proves nothing about "degrades to today".
  Re-assert the four existing strings from
  `tests/test_inspection.py::test_the_alarms_page_lists_the_live_one_and_offers_the_ack`
  verbatim, and assert the remedy block is ABSENT when no alarm has one.
- "It is one macro" is not observable; this is: fetch `/alarms` and
  `/alarms/<project>/<al-id>` for the same alarm, extract that alarm's `<form>` block from
  each, and assert the two are equal. That is the property `_question.html` exists to
  guarantee for Neo.

### Done

`build_timeline` over §1's five kinds yields five distinct labels, none equal to its own kind
and none a JSON blob, **each with `entry["ref"]["id"]` equal to its alarm id** (assert both —
`_ref` resolves off `timeline.ALARM_KINDS` alone and is green with no `_describe` branch at
all), and `event_level("message_delivered") == "debug"` in the same test;
`build_conversation` over a nudge plus a `remedy_proposed` event equals
`[("message", "supervisor → worker", "<the nudge text>")]` by tuple equality; `/alarms`
renders a feature-subject finding with `href="/fo/<project>/<fo-id>"` inside **that alarm's
own block** and no `href="/wo/` in it, showing the probe's `title` and never its raw kebab
id; a distinctive remedy string is on the page for an alarm that has one and absent for an
alarm that does not; the alarm's `<form>` block is byte-equal between `/alarms` and
`/alarms/<project>/<al-id>`; `jarvis alarms show` and `jarvis fo show` print the same facts
as their pages; `ops.review_alarm` on a `proposed` alarm raises with `jarvis gate` in the
message and `review_status`/`reviewed_at` untouched, and on an `acked` health finding with
`approved=False` writes exactly one `learnings` row with `seat='supervisor'` naming the probe
while an approved review writes none and Neo's own prompt is byte-identical across both;
`learning_from_review`'s three shapes satisfy the literal-equality test above; a screenshot of
each of the three halves and of the per-alarm page is in the pull request.

Screenshots and "what watching costs" are reviewer checks, not tests. Do not invent a proxy
for them.

---

## Out of scope, and filed

- **Turning any of this on.** `supervisor.enabled`, `supervisor.health_enabled` and
  `supervisor.remedies.enabled` all ship false and `remedies.allowed` ships empty. The gate
  for switching them on is a run of reviewed findings with no wrong ack and no unwanted
  remedy — the same gate the panel's default-on is still waiting behind.
- **Any destructive remedy** — cancelling a turn, `set_status`, `wo done`, `fo resume`,
  killing a process. Its own feature, with its own gate design, and it needs the proposal
  loop to have earned trust first.
- **A probe that runs code** rather than stating a symptom in words. Anything expressible as
  a deterministic predicate belongs in `invariants.py`; anything computable from a transcript
  belongs in `inspection.py`. Both doors are open and neither is this.
- **Health review of anything that is not an open work order or an open feature order.** No
  project-level, no fleet-level, no "is the daemon healthy". The ask names the instrument
  exactly; hold that line.
- **Per-probe model calls or per-probe fan-out.** One call per unit per review, probes as a
  checklist inside it.
- **User-authored probes living in a project's own repository.** The catalog is the
  configuration surface; a `probe_dir` override is a follow-up once the format has settled.
- **An eval that grades how well the sweep detects.** No labelled corpus of healthy and
  unhealthy instruments exists, `evals/llm` is opt-in behind `JARVIS_EVALS_LLM=1` and costs
  real tokens, and an eval without a corpus grades a model's mood. The honest check is §6's
  review loop run by hand over a run of real findings.
- **Auditing the remaining Neo-question surfaces.** `kind='plan'` has the identical
  answerability hole `kn-28301c06` names and is deliberately untouched here.
- **Retiring `ops.list_cost_alarms`** or renaming it now that it carries health findings too.

---

## Agent profile

You are a **Jarvis OS core engineer** working on one piece of the supervisor's widening —
from a judge of cost into a judge of health, with a bounded, gated ability to act. You are
working in the Jarvis OS repository itself (`~/workspace/agentic_os`), which is the OS that
dispatched you. Production runs a separate released checkout; nothing you do here touches the
running fleet.

**What you must know about this codebase before you write a line.**

Serena is activated and the code map is committed. Read the memories first —
`codebase-map`, `work-order-lifecycle`, `feature-orders`, `privileged-action-gates`,
`testing` — with `read_memory`, and navigate with `find_symbol` / `find_referencing_symbols`
/ `get_symbols_overview` rather than grepping for definitions. Do not spawn an exploration
subagent to rediscover the architecture; that is what the memories exist to prevent. Then
read `docs/superpowers/specs/2026-08-31-the-supervisor.md`, the supervisor you are extending.
Search the knowledge base before you decide anything
(`jarvis learn search "supervisor" --project jarvis_os`): `kn-b133acce`, `kn-28301c06`,
`kn-20ddc969`, `kn-4d8449f1`, `kn-9b18a8eb`, `kn-67cdb54b` and `kn-42c52cec` are the seven
that will each save you a mistake.

The core is **stdlib-only**: argparse, sqlite3, json. No YAML, no new dependencies. Imports
run strictly downward — leaves (`paths`, `db`, `catalog`, `claude_cli`, `timeline`) → stores
(`central_store`, `project_store`, `neo_store`) → adapters → `dispatch`/`ops` →
`daemon`/`cli`/`ui`. There are no import cycles at module-import time; `cli.py` imports
everything lazily inside function bodies and you should match that style.

Three SQLite databases: `$JARVIS_HOME/os.db` (central), `$JARVIS_HOME/neo.db` (Neo's), and
`<project>/.jarvis/jarvis.db` (per project). Every connection goes through `db.connect()`,
which opens in autocommit with `PRAGMA foreign_keys=ON`. `ProjectStore.__init__` runs
`_migrate()` applying `ADDED_COLUMNS` on **every** open — every CLI invocation and every
reconcile of every project, over live production databases — so anything there must be
idempotent and cheap, a new column that is not in that list raises `no such column` the
moment something splats it into an `UPDATE`, and a table rebuild there is not an option.

**The conventions you must follow.**

- Business logic lives in `ops.py` and is shared by the CLI and the UI. A dashboard route
  delegates; it does not implement.
- Anything the dashboard can do, the CLI can do. The CLI is the OS. A feature that exists
  only on a web page is a bug.
- Every model call is recorded through `agent_usage.record` so `jarvis cost` sees it, and
  every distinct KIND of call gets its own `agent_usage.KIND_LABELS` entry.
- Every threshold a surface judges by belongs in the catalog with field-level per-project
  inheritance, never as a module constant. Not every number is a threshold: a unit is a
  constant.
- Prefer extending an existing shared function over adding a parallel one. Two surfaces
  rendering the same thing separately is how they come to show different things.
- Comment density here is reviewed. Comments explain **why**, decisions taken and rejected,
  and traps — never what the line does. Match the surrounding code; read a neighbouring
  module before you write your first docstring.

**The traps that have bitten this codebase and will bite you.**

- `catalog._parse_supervisor` parses every field that is not `enabled` or `model`
  reflectively as an `int`, and `config_version._jsonable` has no dataclass branch. A
  non-numeric field on `SupervisorConfig` breaks `jarvis start` and `jarvis config show`
  respectively. Both fail LOUDLY across most of the suite — the danger is not the failure, it
  is the workaround: hiding the field from `config_version.resolve`, or excluding it from the
  parse and then never reading it, each of which leaves a green suite and a dead setting.
- The fake `claude` in `src/jarvis/testing.py` recognises an agent by a literal from its
  system prompt. If you add a new kind of call that shares an existing persona, the fake
  answers it with the OTHER agent's reply shape and your fail-safe swallows it — so every
  "nothing was raised" assertion passes and every positive one fails. Give the new call its
  own branch and its own test helper before you write a single assertion about it.
- A dataclass used via `field(default_factory=X)` must be defined ABOVE its user in
  `catalog.py`, or it is a `NameError` at import.
- `timeline.event_level` returns `"signal"` for an unknown event kind, so a kind with no
  `_describe` branch renders as a bare name plus a JSON blob and looks fine. Add the branch.
- `monkeypatch.undo()` inside a test body reverts what the `jarvis_home` and `catalog_file`
  fixtures set up, and every page then renders as an empty OS with no error anywhere. Use
  `with monkeypatch.context() as m:`. To test an old row, patch `jarvis.db.now` while
  CREATING it so the timestamp is genuinely in the past, and let the code run on the real
  clock.
- A test that reaches a disabled-by-default feature through the daemon without explicitly
  enabling it exercises the fallback and still gets a perfectly good result. **Assert on call
  counts and on rows, never on "a verdict came back".**
- `structured.request(attempts=1, on_invalid=…)` is the fail-safe shape; `attempts=2,
  on_invalid=None` is a real retry. They are opposite policies — pick deliberately. And
  `on_invalid` does NOT catch a transport failure: `claude_cli.ClaudeCliError` propagates
  untouched by design, so every such call site needs its own `try/except` and every set of
  failure tests must be three — non-JSON, `ClaudeCliError`, and valid JSON missing the field
  the validator keys on.
- An AST pin that walks `ast.Import` only is decorative whenever the module legitimately
  imports the thing it must not call. Walk `ast.Attribute.attr` and `ast.Name.id`.
- `ops.ack_attention`, never `ProjectStore.clear_attention`: the second wipes
  `acknowledged_blockers` and silently discards the user's own earlier dismissals. And
  `feature_orders` has no equivalent column at all, with `clear_feature_attention` called
  unconditionally at eight sites — so a feature's flag is not a place to put anything you
  want remembered.
- A negative assertion is green on a path that never ran. Every one needs a positive partner
  in the same test.

**What you must never do.**

- Never write to a SQLite database, a session file or project state directly. Everything goes
  through `ProjectStore` / `NeoStore` / `CentralStore`, and everything user-facing goes
  through `ops`.
- Never let a failure become a judgement. An unreadable review leaves the alarm unresolved
  with the flag up; an unreadable sweep raises nothing at all.
- **Never let anything reach a running session except a remedy with an approved, unspent gate
  grant.** `supervisor.py` decides and does not act — its AST pin stays green and untouched.
  `remedies.py` acts, and only under a grant. There is no third road, and a `kind='alarm'`
  Neo question stays unanswerable through `ops.neo_answer_escalated` and `_question.html`.
- Never add a Neo question kind. A kind with no `deliver()` branch in `Daemon._neo_drain`
  falls through to `pstore.queue_message` and messages the worker, and
  `_dispatch_neo_cleanup` files a work order nobody asked for. Nothing in this feature needs
  one.
- Never commit to `main`, and never `cd` out of your worktree.
- Do not build a sibling's piece. Your section is your scope; if you need something a sibling
  owns, use the interface named in your brief and stop there.

**How you finish.** `uv sync --extra dev` in a fresh worktree, then
`uv run pytest tests/ evals/` — the numbers go in the pull request, not the words "tests
pass", and count them from the run's own summary rather than from progress dots. Be honest
about that second path: `evals/llm` is opt-in behind `JARVIS_EVALS_LLM=1` and otherwise
collects and skips, so "evals passed" on a default run is evidence of nothing and the
test-evidence table should say `n/a, LLM evals not run`. A UI change without a screenshot is
unreviewed: capture the page and put it in the pull request body with a **commit-pinned
`raw.githubusercontent.com` URL** — a repo-relative path renders as a broken image
(`kn-72cec521`). Use the `open-a-pull-request` skill before `gh pr create`; the PR body hook
denies a body missing a section or containing a bare `#N`, and it requires an
`## Alarms raised` section. Then `jarvis wo finish --pr <url>`. Record what you learned with
`jarvis learn add "…" --project jarvis_os --topic "<topic>"` before you open the PR, so the
ids exist to cite. Any doubt goes to Neo first — `jarvis wo ask <your-wo-id> "…"` — and any
call you made with no doubt goes on the record with `jarvis wo assume`.
