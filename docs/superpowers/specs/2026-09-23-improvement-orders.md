# Improvement orders: an order type that diagnoses root causes and proposes orders

Status: specification for feature order `fo-085423aa`. Written 2026-09-23.

## 1 — What an improvement order is, and what it is not

An IMPROVEMENT ORDER analyses observed OS misbehaviour, finds the ROOT CAUSE, and
PROPOSES the orders that would fix it. It never writes a code diff, never opens a pull
request, and never fixes the thing it is looking at.

It exists because of a measured failure. Work order `wo-dd8668fa` (tracker issue #575, the
background-job promise) delivered a fix that turned the ticket green and treated the
SYMPTOM: workers run under `claude -p`, so a background task dies when the turn ends, and
the delivered fix re-sends feedback asking the worker to redo the work in the foreground —
costing a full turn every time it triggers — instead of instructing workers never to spawn
a background task at all. Two structural reasons, and both are about incentives rather
than about that one worker:

* A work order rewards closing the ticket, so the cheapest fix that goes green wins.
* The validation panel judges "does it work", not "is this the right fix".

So the remedy is a different ORDER TYPE with a different posture, not a better prompt on
the same one. Its agent is an analyst: it is rewarded for a diagnosis, it is required to
argue AGAINST the obvious fix, and its output is reviewed as a diagnosis.

### 1.1 Vocabulary

| Term | Meaning |
|---|---|
| improvement order | the unit. Id prefix `io-`. CLI namespace `jarvis io`. |
| evidence | what it is given: references to existing OS records plus the user's observation |
| the analyst | the one work order an improvement order dispatches (`kind='analyst'`) |
| the report | the analyst's structured output: a summary plus a list of findings |
| a finding | symptom + root cause + why the obvious fix is insufficient + recommendation + proposed orders |
| a proposed order | a work order or feature order the finding recommends filing. NOT created until that finding is accepted. |

### 1.2 The shape, in one line each

```
jarvis io create jarvis_os "title" -d "<observation>" --ref wo-dd8668fa --ref '#575'
  -> Daemon.plan_features opens ONE analyst work order        io: pending -> planning
  -> the analyst reads the evidence THROUGH THE CLI, writes report.json
  -> jarvis io report <io-id> --from-file report.json          io: -> plan_review
       validated by findings.py (pure, no LLM), stored, and the analyst is SETTLED
  -> the USER decides each finding                             jarvis io review
       accept -> a knowledge-base entry + that finding's proposed orders are filed
       reject -> the reason teaches Neo
  -> every finding decided                                     io: -> completed
```

### 1.3 Four things it deliberately does NOT do

* **It does not own the orders it proposes.** A proposed order is filed as an ORDINARY
  independent work order or feature order with a back-link, never as a child carrying
  `parent_id = <io-id>`. Section 5.3 is the argument; this is the single most load-bearing
  decision in the design.
* **It does not ask Neo anything.** The report goes straight to the user. Auto-acceptance
  is left a seam and nothing is built for it (section 5.5).
* **It does not run a validation panel.** There is no pull request to judge. An improvement
  order never reaches `executing` or `validating`.
* **It does not write its own learnings.** The OS writes them, from findings the user
  accepted (section 5.4). An analyst that wrote its own would put rejected findings into
  every future worker's prompt.

### 1.4 Naming

`improvement_order` is kept over `investigation_order` / `analysis_order`, although the
agent improves nothing itself: the user named the artifact after what it produces, and it
is the user's call. No collision — `db.new_id` uses `wo`, `fo`, `kn`, `bl`, `al` and not
`io`, and argparse subparsers do not abbreviate, so `inbox`, `inspect` and `issues` are
spelled in full and `io` is free. One cost to pay in prose: the crib sheet must say `io` is
"improvement order", not input/output. Where the DISTINCTION matters the prose says "the
analyst", which is honest about the agent without renaming the order.

## 2 — Persistence and the CLI surface

### 2.1 A kind on `feature_orders`, and the columns it reuses

An improvement order is a row in the EXISTING per-project `feature_orders` table
(`src/jarvis/project_store.py:551`), discriminated by a new `kind` column. Not a new table,
and — this is the load-bearing half — not new columns either. It REUSES three:

| Column | For a feature order | For an improvement order |
|---|---|---|
| `plan_wo_id` | the planner | the ANALYST |
| `plan` | the submitted plan JSON | the findings report JSON |
| `plan_question_id` | Neo's plan review | NULL, always, for now (section 5.5) |

That reuse is why the expensive half of the acceptance list needs no code at all:
`budget._family` (`budget.py:213`), `bill.for_feature_order` (`bill.py:1142`), `bill.build`
(`bill.py:799`), `ops.find_feature_order` (`ops.py:4795`), `feature_orders.budget_usd`,
`Daemon.seal_bills` and the `feature_order` alarm subject all key on "a feature-order row
with a `plan_wo_id`", and an improvement order is one. A separate table would have to
re-earn every one of them.

```
kind TEXT NOT NULL DEFAULT 'feature'      -- FO_KINDS
```
Added through the existing `ADDED_COLUMNS` migration for `feature_orders`, so every
pre-existing row reads back as `kind='feature'` with no backfill. Declare
`FO_KINDS = ("feature", "improvement")` beside `FO_STATUSES`. Per `kn-c712a5d6` the column
is untested until a test WRITES both values and a test reads a row that PREDATES it.

The evidence refs go in `feature_orders.metadata` under an `evidence_refs` key: a JSON list
of the `--ref` strings, verbatim.

### 2.2 Statuses are REUSED VERBATIM, and fixed at render time

`FO_STATUSES` (`project_store.py:383`) is not extended and no `IO_STATUSES` tuple is
added. `planning` is the analyst running and `plan_review` is the report awaiting the user;
`pending`, `completed`, `failed`, `cancelled` and `budget_exhausted` mean what they always
did; `executing` and `validating` are never reached.

Three reasons this is right even though `planning` reads wrong for an investigation:
`set_feature_status` (`:2246`) asserts membership in that tuple; `FO_TERMINAL_STATUSES`
drives seal, settle and `specs.remove_agent`; and a second tuple forces every
`statuses=FO_OPEN_STATUSES` caller to take a union, which is a new way for the two kinds to
disagree about what is open.

So the fix is a KIND-AWARE LABEL, in the render funnels and nowhere else. **The mapping
lives in ONE module and all three renderers import it:** declare
`FO_STATUS_LABELS: dict[str, dict[str, str]]`, keyed by kind then status, in
`project_store.py` immediately beside `FO_STATUSES` — a leaf module every renderer may
already import, which is the only arrangement in which "one mapping" is a property rather
than a wish. `planning` -> "analysing", `plan_review` -> "findings awaiting you"; a kind
with no override falls through to the status string itself, so the feature-order labels are
unchanged by construction. The three readers are the status column of `jarvis io list` /
`show`, `ops.os_status`'s `f"feature:{fo['status']}"` label (`ops.py:568`), and the UI's
feature-status metadata (`ui/app.py:79`).

### 2.3 The report, and where a finding's decision is stored

The whole report lives in `feature_orders.plan` as JSON (section 4.2), and a finding's
DECISION is written back into that same document: `status` (`pending` | `accepted` |
`rejected`), `decided_by`, `decided_at`, `feedback`, `knowledge_id`, `created_order_ids`.

One document rather than an `io_findings` table, deliberately: the decisions are written by
exactly one function (`ops.review_findings`) and read by exactly two renderers, so a table
would buy a migration, five store verbs and a second place for the report and its verdicts
to disagree. The store verb is `ProjectStore.update_feature_order(fo_id, plan=…)`, which
already exists. A finding is addressed by its `key` (section 4.2), unique within the report.

### 2.4 The feature-order queries that have NO kind filter, and leak

`kn-52e51faf` records that every WORK-order kind filter in this codebase is POSITIVE, so a
new work-order kind is excluded for free. **The inverse is true here: nothing reads
`feature_orders.kind` today, so a new kind leaks into every one of these.** Each needs an
explicit positive filter and a test that an improvement order is not picked up:

| Symbol | What leaks without the filter |
|---|---|
| `Daemon.plan_features` (`daemon.py:892`) | files a `kind='planner'` work order with the PLANNER prompt at an improvement order. The loudest failure, and why the filter is not optional. |
| `ProjectStore.list_feature_orders` (`:2207`) | improvement orders show up in `jarvis fo list`, and through `ops.os_status:536` in the status summary |
| `ProjectStore.feature_status_counts` (`:2216`), `feature_summary` (`:2434`) | the two kinds' counts merge |
| `ProjectStore.flagged_feature_orders` (`:2224`) | the attention line says "plan needs your review" about a report |
| `ops.submit_plan` / `ops.review_plan` | `jarvis fo plan` accepted against an improvement order |
| `ops.resume_feature_order` | `jarvis fo resume` offered for an order that has no children |

`list_feature_orders` grows a `kind` parameter DEFAULTING TO `'feature'`; defaulting to
"everything" is what makes the leak silent. `ops.os_status`'s attention rollup and
`flagged_feature_orders`' own flag-reading must still SHOW improvement orders — an
improvement order needing the user is exactly as much an attention item as a feature order
is, so the filter goes on the listings and not on the attention path.

Verified as needing NO change, and each is worth a pinning test rather than a rewrite:
`feature_children` (`:2272`) filters `kind='worker'`, so the analyst is excluded as the
planner is; `claim_next_pending` (`:1819`) scopes its `max_parallel` clause to
`w.kind='worker'`, so an analyst is exempt for free; `specs.remove_agent` no-ops with
nothing installed; `Daemon.settle_features` and `_route_to_validation` scan only
`executing`, which an improvement order never reaches (section 5.3 is why).
`ProjectStore.feature_order_for_planner` (`:2344`) matches on `plan_wo_id` and will now
match analyst rows too — believed harmless, and `INV-NEO-ESCALATIONS-LIVE` reads it, so
prove it with a test rather than reasoning.

### 2.5 Two id-prefix sites

`ops.py:2095` and `ops.py:2388` dispatch on `unit_id.startswith("fo-")` to decide whether
an id names a feature order, and `cli.cmd_cost` (`cli.py:1933`) tests
`target.split("-")[0] in ("wo", "fo")`. All three must accept `io-`. Use one shared
predicate rather than a third literal in each place.

### 2.6 The CLI

`jarvis io <verb>`, a new top-level subparser in `src/jarvis/cli.py` (`cmd_io`), placed
after `cmd_fo` and modelled on it verb for verb: a user who knows `jarvis fo` must not have
to learn a second shape. This section owns these four verbs and the budget passthrough;
`report` belongs to section 4 and `review` to section 5.

```
jarvis io create <project> "title" -d "<observation>" [--ref <id|#n|url> ...] [--budget USD]
jarvis io list [project] [--all] [--json]
jarvis io show <io-id> [--json]
jarvis io cancel <io-id>
jarvis io budget <io-id> [<usd>] [--clear]
```

`ops.create_improvement_order` refuses, with the fix named in the message, when `-d` is
empty (the analyst's first reader is a fresh session with no memory of the conversation),
when the project is not registered, and — the one refusal a feature order has no analogue
for — **when no `--ref` was given: an improvement order with no evidence is a request for
an opinion.**

A `--ref` is stored verbatim and is NOT resolved or validated at creation. The analyst
resolves them, and a reference that does not resolve is a FINDING about the OS's records,
not a CLI error. Documented but unenforced shapes: a `wo-`/`fo-`/`io-`/`al-` id, `#<number>`
for a tracker issue, a `https://` URL, free text.

`--budget` and `jarvis io budget` reuse `budget.feature_default_for` and
`ops.set_feature_budget` unchanged — the family is the order plus its analyst, and the
family arithmetic is already correct with no children.

`show` renders COUNTS FIRST — `3 findings: 1 accepted, 1 rejected, 1 awaiting you` — then
the observation, the evidence refs, the analyst's id and status, then one block per finding
through the shared renderer of section 4.4. `cancel` stops the analyst if one is running
and moves the order to `cancelled`; it does NOT touch orders already filed from accepted
findings, which are independent by construction and represent a decision the user already
took.

### 2.7 One shared test fixture, landed here

Every later piece of this feature needs "an improvement order that exists", and divergent
local copies are how a suite starts disagreeing with itself. So a fixture named
`improvement_order` lands in `src/jarvis/testing.py` — where this repo's shared fixtures
live — as part of THIS section: a filed improvement order with its evidence refs, in the
`project` fixture's project, with no analyst and no report. Later pieces build on it rather
than re-file one.

`tests/test_improvement_orders.py` is this section's own test file, and no other piece of
this feature adds to it.

## 3 — Dispatch and the analyst briefing

### 3.1 One more work-order kind

`WO_KINDS` (`project_store.py:254`) gains `"analyst"`. `kn-52e51faf` is the map of what
that costs, and it is to be verified rather than assumed:

* `ProjectStore.count_active` has NO kind filter, and that is CORRECT here — the analyst is
  short-lived like a planner, so it should spend a project slot while it runs.
* `dispatch.build_worker_prompt` (`dispatch.py:290`) branches `planner` / `manager` /
  else-worker, and `supervisor` (`supervisor.py:314`) branches the same way. Both need an
  explicit `analyst` branch, or the analyst silently receives the WORKER contract and is
  told to open a pull request — the one thing it must never do.
* `bootstrap.install_agent_assets` (`bootstrap.py:108`) hands the two planning seats to
  `kind == "planner"` only. LEAVE IT ALONE: an analyst gets no seats. A diagnosis is one
  reading of one set of records, and the seats exist to argue about a DECOMPOSITION, which
  an analyst does not produce. It also could not use them — they have no `Bash`, and
  reading the evidence is all shell.

`Daemon.plan_features` grows a sibling loop with the same idempotent-by-status shape: for a
`kind='improvement'` row in `pending`, create ONE work order with `kind='analyst'`,
`parent_id=<io-id>`, the observation verbatim as its description; store it in `plan_wo_id`;
move the order to `planning`. The order leaves `pending` in the same call, so a tick that
crashes between the two files the analyst again next time rather than stranding the order.

### 3.2 `dispatch._analyst_prompt`

A sibling of `_planner_prompt`, and a STATIC prompt: there is no spec to build an agent type
from, so `specs.install_agent` is never called for an improvement order. It includes
`_common_briefing` (`dispatch.py:349`) — the knowledge index and the navigation ranking —
and then five things, each there because of a way the analysis fails:

1. **The posture.** "You are an ANALYST. You do not fix anything. You produce a diagnosis.
   A session that returns a working fix has failed even if the fix is good." With the
   corollary this whole feature exists for: **argue against the obvious fix.** For every
   finding, state the cheapest fix that would turn the symptom green and say why it is
   insufficient. A finding whose recommendation IS the cheapest fix must say so and say why
   that is nevertheless right.
2. **Read the evidence through the CLI, never the databases.** `jarvis wo show`,
   `jarvis wo list`, `jarvis inspect`, `jarvis validation show`, `jarvis cost`,
   `jarvis alarms`, `jarvis issues`, `jarvis learn search`/`show`, and `gh pr view`/`diff`
   for a pull request. Prime directive 1 applies to it as it does to everyone.
3. **Evidence must be QUOTED.** A root cause asserted without a verbatim line from a
   timeline, an `inspect` reading or a validation transcript is an opinion. The validator
   enforces that the field is non-empty; the prompt is what makes the quotes real rather
   than paraphrased.
4. **The output shape and the terminal action.** `jarvis io report <io-id> --from-file
   report.json` IS its `jarvis wo finish` — it must not call `wo finish`. `--from-file` and
   not an inline argument, for the same reason the planner uses one:
   `gates.scannable()`'s quote-blanking fails on nested and mixed quoting, and a report is
   a long argument full of repo paths and quoted log lines, which is the input that trips
   the gate classifier into a false positive.
5. **Scope discipline.** It proposes orders; it does not file them. It writes no product
   code and opens no pull request. At most `findings.MAX_FINDINGS` findings, ranked.

It keeps ordinary worker permissions, like a planner and for the same reason: it must be
able to write the `report.json` it submits, so there is no `permissions.deny` that both
stops product code and leaves it able to work. "You analyse, you do not build" is prose,
stated plainly as prose rather than claimed as enforcement.

### 3.3 The evidence block

`dispatch` renders the stored `evidence_refs` into the prompt as a checklist: one line per
ref, with the command that reads that shape (`jarvis wo show <id>`, `gh issue view <n>`,
`jarvis inspect <id>`). Resolution is the analyst's job; naming the command is the OS's, so
the analyst does not spend its first turn guessing which verb reads which kind of reference.

## 4 — The findings report and its validator

### 4.1 The submit verb

`jarvis io report <io-id> --from-file <path>` -> `ops.submit_findings(io_id, doc,
project_name=None)`, modelled step for step on `ops.submit_plan` (`ops.py:4920`). The ORDER
of the steps is the design:

1. validate (`findings.parse_report`), so a bad report costs the analyst one revision and
   nothing else — nothing stored, no user attention spent, no state to unwind;
2. store it into `feature_orders.plan`, every finding `status='pending'`;
3. move the order to `plan_review` and raise ONE attention item on it (section 6.2);
4. settle the analyst with `ops.finish` — because `jarvis io report` IS its finish — **but
   only if the analyst is still open.** A resubmission arrives while the order is
   `plan_review`, by which time step 4 has already settled it once, so this step is
   CONDITIONAL and a second submission leaves the settled analyst alone and omits the
   `analyst` key from the return value. Settling an already-settled work order is not an
   idempotent no-op in this codebase and must not be attempted.

Accepted while `planning` or `plan_review` — a resubmission REPLACES the stored report
wholesale — and refused in any other status with the status named. Returns
`{"project", "io_id", "status": "plan_review", "findings": N}` plus `"analyst"` on the
first submission only.

A resubmission discards every decision already recorded on the old findings, and the
refusal above is what keeps that safe: nothing can be resubmitted once the order has left
`plan_review`, which it does as soon as the last finding is decided.

### 4.2 The document

```json
{
  "summary": "one line: what is going wrong, across all findings",
  "justification": "TOP-LEVEL and optional: why this needed more than MAX_FINDINGS findings",
  "findings": [
    {
      "key": "background-jobs",
      "symptom": "what was observed, and where",
      "root_cause": "why it happens, in mechanism terms",
      "evidence": [
        {"source": "jarvis wo show wo-dd8668fa", "quote": "the verbatim line"}
      ],
      "why_insufficient": "the cheapest green-making fix, and why it is wrong",
      "recommendation": "what to do instead",
      "proposed_orders": [
        {"type": "work", "project": "jarvis_os", "title": "...",
         "description": "the full brief, standing alone"}
      ]
    }
  ]
}
```

`key` is a short lowercase slug, unique within the report, and it is how `jarvis io review`
and the dashboard address one finding — the same role `key` plays in a plan. `type` is
`work` or `feature`.

`proposed_orders` MAY be empty. A finding whose recommendation is "do nothing, and here is
why" is legitimate and valuable, and forcing an order out of it is how an analyst is pushed
into inventing work.

### 4.3 `src/jarvis/findings.py` — pure, no LLM, no database

A new module beside `plans.py`, and the same argument makes it load-bearing: a check on a
structured document the user is about to act on must be more trustworthy than the thing it
checks. Pure functions, stdlib only, importing `plans` and nothing else from the package.
`FindingsError` carries `problems` as a LIST so every problem is named at once and one
revision fixes all of them.

`parse_report(raw) -> dict` refuses:

* a missing or empty `summary`; no findings at all; a missing, malformed or duplicated
  `key`;
* more than `MAX_FINDINGS = 6` findings unless the report carries a `justification`. Same
  shape as `plans.CHILD_CAP`, and for the same reason: this is the ATTENTION cap, and a
  report the user will not read changes nothing;
* any of the four prose fields (`symptom`, `root_cause`, `why_insufficient`,
  `recommendation`) missing or under `MIN_FIELD_CHARS = 40`. Short prose is the failure mode
  for `why_insufficient` specifically — "it does not fix the root cause" is a restatement,
  not an argument;
* `evidence` empty, an entry with no `source` or no `quote`, or a `quote` under
  `MIN_QUOTE_CHARS = 20`;
* a proposed order with no `title`, a `type` outside `("work", "feature")`, a non-string
  `project`, or a `description` failing `plans._description_problems` — reused rather than
  reimplemented, because that description becomes a real brief read cold by a stranger and
  there is one standard for that in this codebase. **Inherit its trap with it: ORDINALS ARE
  NOT OUTWARD REFERENCES** ("the first step is to add the column" is how a good standalone
  brief opens), so keep a negative control next to every rejection, as
  `tests/test_plan_validator.py` does.

### 4.4 Rendering

`render_finding(finding)` and `render_report(report)` return line lists and are shared by
`jarvis io show` and the dashboard page, so the two cannot drift.
`review_headline(io, report)` returns the counts-first line the attention reason uses.

This section also lands `a_report(**overrides)` in `src/jarvis/testing.py` beside the
`improvement_order` fixture: a document `parse_report` accepts, every field overridable so a
test can break exactly one. The later pieces that need a stored report use it.

## 5 — Review, proposed orders, and auto-learnings

### 5.1 The verb

```
jarvis io review <io-id> [--accept <key>]... [--reject <key>]... [--feedback "why"] [--accept-all]
```
`ops.review_findings(io_id, accept: list[str], reject: dict[str, str], decided_by="user",
project_name=None)`. A separate function from `ops.review_plan` and NOT an extension of it:
`review_plan` takes one `accept: bool` for a whole plan, an improvement review is PER
FINDING, and forcing both through one signature re-opens exactly the "two chances to
disagree about what releasing means" risk that made `review_plan` one function in the first
place.

Refusals: an unknown key; a key already decided, UNLESS that finding carries a filing error
(section 5.2), in which case re-accepting it retries the filing and writes no second
knowledge entry; a rejection with no feedback (the feedback is the entire teaching signal,
as it is for `review_plan`); `--accept-all` combined with a rejection (a blanket rejection
with one reason is a rejection nobody can learn from).

### 5.2 What a decision does, in order

On ACCEPT, per finding:

1. write the knowledge entry (section 5.4) and record its id on the finding;
2. file each proposed order — `ops.create_work_order` for `type='work'`,
   `ops.create_feature_order` for `type='feature'`, in the finding's own `project` —
   collecting the ids into `created_order_ids`;
3. mark the finding `accepted` with `decided_by`, `decided_at`, `feedback`.

On REJECT: mark it `rejected` with the feedback, and teach Neo (section 5.5).

Then, once per call: if no finding is left `pending` AND nothing failed to file, move the
order to `completed` and clear its attention flag. An order whose every finding was
rejected still completes — it did its job, which was to produce a diagnosis, not to be
agreed with.

FAILURE POLICY, following `kn-652456b8`'s durable/best-effort split: the DECISION and the
knowledge write are DURABLE; a proposed order that cannot be filed (an unregistered
project, say) records the error on the finding and leaves the decision standing. A finding
the user accepted is accepted whether or not the OS could file the work.

**A filing failure is never silent, and this is the correction to the obvious version of
the rule.** If the decision were durable and the flag went down anyway, the user would
accept a finding, the OS would fail to file its work order, and the only record would be a
return value nobody reads. So a filing failure KEEPS THE ATTENTION FLAG UP — with a reason
naming the finding and the error — and raises one inbox row through `CentralStore.add_inbox`
so it reaches the user's sinks. The order stays in `plan_review` until the filing succeeds
on a retried `jarvis io review` of that finding, which is allowed precisely and only for a
finding whose decision stands but whose orders did not file.

Returns `{"created": [{"id", "type", "title"}], "learnings": [kn-ids], "errors": [...],
"status"}`.

### 5.3 Why a proposed order is NOT a child

**This is a DELIBERATE DEPARTURE from the wording of the ask**, which says "child-order
creation on approval", and it was put to the user's delegate and confirmed (Neo, 2026-09-23).
The reason is the first bullet below and it is mechanical rather than a preference: a
proposed order may be `type='feature'`, and a FEATURE-TYPE PROPOSAL HAS NO PARENT SLOT —
`work_orders.parent_id` references `feature_orders(id)` and nothing references it from
`feature_orders`. So "children" could only ever have meant half the proposals, and half a
rule is worse than none.

A proposed order is filed with `parent_id` NULL and a back-link — the improvement order's id
in the new order's `metadata` under `origin_io` — never as a child of the improvement order.
Four reasons, and the first is fatal on its own:

* **A feature order cannot be a child of a feature order.** `work_orders.parent_id`
  references `feature_orders(id)`, so a proposed order of `type='feature'` has no parent
  slot at all. Half the proposals being children and half not is worse than none being
  children.
* Children of a feature order are dispatched with `--agent <fo-id>` and a materialised
  `spec_section` (`worker_session.feature_agent`, `dispatch.materialize_design_doc`),
  neither of which an improvement order has. Making them children means either a fake spec
  or a second dispatch path.
* `Daemon.settle_features` would hold the improvement order open until every proposed fix
  had merged, so a diagnosis accepted today stays open for weeks with an attention line
  saying something false about what the user owes. It also drags the order through
  `executing` and `_route_to_validation`, which would open a validation round on an order
  that has no pull request.
* The fix for a root cause is frequently in ANOTHER project. A child must live in its
  parent's project; a back-linked order need not, which is what keeps this design
  project-agnostic.

### 5.3.1 The back-link must be readable from BOTH ends

A back-link nothing renders is a column, not a link, and this is the condition the departure
above was confirmed under (Neo, 2026-09-23). Two obligations, and neither is optional:

* **From the improvement order:** `jarvis io show` and the order's dashboard page list, under
  each accepted finding, every order it filed — id, type, title, AND ITS CURRENT STATUS,
  read live from the store rather than from the snapshot taken when it was filed. So the
  user can see at a glance which of the fixes a diagnosis proposed are done, running or
  still pending. A filed order that has since been deleted renders as deleted rather than
  vanishing from the list.
* **From the filed order:** it names the improvement order it came from in the FIRST LINE of
  its description, so a worker dispatched to it knows which diagnosis asked for it and can
  run `jarvis io show <io-id>` to read the root cause it is fixing — and `metadata.origin_io`
  carries the id for anything querying rather than reading.

The statuses are gathered ONCE, by a `filed_orders` resolution added to
`ops.show_improvement_order`, and passed to both renderers so the CLI and the page cannot
disagree. Cross-project proposals make this a fan-out across project stores: resolve each id
with `ops.find_work_order` / `ops.find_feature_order` rather than assuming the improvement
order's own store holds it.

WHO BUILDS WHAT: the resolution and the CLI half belong to THIS section, because they cannot
be written before a finding can be accepted. `ops.show_improvement_order` itself already
exists from section 2.6 and rendered nothing here, because no finding could be accepted yet;
this section extends it. The dashboard half consumes the same `filed_orders` payload and
belongs to section 6.1.

### 5.4 Auto-learnings

Every ACCEPTED finding becomes exactly one knowledge-base entry, written through
`ops.learn_add` and never `CentralStore.add_knowledge` directly — `ops.learn_add` is what
attributes the write and records the timeline side effect (`kn-652456b8`). `project` is the
improvement order's project, `topic` is `os-failure-mode`, `tags` carries the improvement
order's id so every entry one diagnosis produced is a single search, and `wo_id` is the
analyst's.

The content is composed by `findings.knowledge_text(finding)`, and its FIRST LINE is the
ROOT CAUSE STATED AS A RULE. Per `kn-8656d497` and `kn-0281d10b`, entry bodies never reach
a worker's prompt — an index of first lines does, truncated to 160 characters — so the first
line is the only part most workers ever see unprompted. The entry opens
``WORKERS RUN UNDER `claude -p`: NEVER SPAWN A BACKGROUND TASK…``, not "This finding is
about…". The remaining fields follow as labelled paragraphs.

### 5.5 Neo: rejections teach it, and nothing else is built

A rejection adds a Neo learning (`NeoStore.add_learning`) carrying the user's feedback and
the finding's root cause, scoped to the project — the same path
`jarvis wo review --feedback` uses for a correction.

AUTO-ACCEPTANCE IS A SEAM AND NOTHING IS BUILT FOR IT. No `kind='findings'` question, no
config key, and `plan_question_id` stays NULL for every improvement order. A question the
OS would always escalate costs a model call and gives the user a second thing to close, and
a config key with no implementation behind it is dead config. What the seam IS, recorded
here so a later work order does not have to rediscover it: a fourth `neo_store.Q_KINDS`
member, a `_deliver_findings_verdict` sibling of `Daemon._deliver_plan_verdict`
(`daemon.py:2831`), and an ACCEPT-ONLY verdict — never a rejection, and never for a finding
whose proposed orders would need a gated action.

## 6 — Surfaces: the page, attention, and cost

### 6.1 The dashboard page

`GET /io/{project}/{io_id}` -> `ui/templates/improvement_order.html`, modelled on
`feature_order.html`. It renders the counts-first line, the observation, the evidence refs,
the analyst's status linked to its work-order page, and one card per finding showing all
five fields plus its proposed orders. An ACCEPTED finding's card additionally lists the
orders that finding FILED, each linked to its own page and showing its CURRENT status, read
from the `filed_orders` payload `ops.show_improvement_order` already resolves — never
re-resolved here, or the page and `jarvis io show` become two answers to one question. That
list is the other end of the back-link and section 5.3.1 is why it is required rather than
nice. A PENDING finding's card carries the two POST
actions — accept, and reject with a REQUIRED reason textarea — so the decision is taken
where it is read. The POST routes delegate to `ops.review_findings` and hold no logic of
their own, as every existing UI action does.

The project page lists open improvement orders in their own short block beside the
feature-order block, with the same "counts, not trees" rule: findings are not expanded
there, because that is what the order's own page is for.

### 6.2 Attention

Exactly ONE attention item per improvement order: raised by `ops.submit_findings` when the
report lands, cleared when the last finding is decided. Its reason is
`findings.review_headline`, so it leads with counts. Three rules:

* It is written with the existing `flag_feature_attention` / `clear_feature_attention` pair,
  and `ops.os_status`'s rollup collapses the analyst's own line under it exactly as it
  collapses a feature's children. **There is NO ack verb for it, and that is correct rather
  than an omission:** `ops.ack_attention` takes a work-order id and a feature-order-level
  flag has never had one, because the flag means "a decision is owed" and the way down is to
  take the decision. `jarvis io review` is the only thing that lowers it — the same rule
  `jarvis wo ack` already enforces for pending assumptions, which it refuses to bury. So the
  flag-once property is proven without an ack: raise it, run `daemon.tick()` repeatedly, and
  assert nothing re-raises or re-writes it; then decide the findings and assert it is down
  and stays down.
* The `decide` string `jarvis status` prints must name `jarvis io review <io-id>`. An
  attention item whose instruction names no command is one the user has to guess at.
* **Flag-once must be true BY CONSTRUCTION**, as it is for `settle_features`: the order
  leaves `planning` in the same call that raises the flag, and NOTHING re-derives
  improvement-order attention on a tick. Per `kn-089de524`, a flag written on a path that
  re-derives every tick re-raises itself and overwrites the user's ack.

An improvement order in `planning` raises nothing. An analyst that is working is the system
working.

### 6.3 Cost

`jarvis cost io-…` and `/cost/<project>/io-…` work through `bill.for_feature_order`
unchanged once section 2.5's prefix predicate is in place: the family is the order plus its
analyst, and the rollup already sums a parent row's own `agent_calls` with its work orders'.
What needs proving with a test is that a COMPLETED improvement order's bill SEALS and that
the analyst's session appears as the worker half — a new kind producing a bill that
silently reads zero is the one failure here that nothing else would catch.

## 7 — Dogfood and the operator documentation

### 7.1 The crib sheet

`CLAUDE.md`'s command crib sheet gains a `jarvis io …` block next to `jarvis fo`, saying
what an improvement order is for ("when the OS misbehaves and you want the cause, not a
patch"), that `io` means improvement order, and the five verbs.

TRAP, and it is a regression risk rather than a style note:
`evals/llm/test_jarvis_judgment.py:24` loads `CLAUDE.md` as a bare system prompt with no
repo context and LLM-grades the operator persona across 14 scenarios. So the block must be
short, operator-facing, and must not push the prime directives down the page. Run that eval
(`JARVIS_EVALS_LLM=1`), or state plainly that it was not run and why.

### 7.2 The dogfood run

File the first real improvement order against the motivating case and let it run for real:

```
jarvis io create jarvis_os "wo-dd8668fa fixed the symptom, not the root cause" \
  -d "<the two defects, in the user's words>" \
  --ref wo-dd8668fa --ref '#575' --ref '<the pull request url>'
```

**The child is DONE when the run happened and its output is quoted verbatim in the pull
request** — not when the report says a particular thing. What the analyst concludes is a
model judgement and cannot be a done condition without blocking this child on
nondeterminism.

What the run is MEASURED against, for the pull-request reviewer rather than for the child:
the report should contain both defects the user identified — that the spec never said HOW the
problem would be fixed, and that the fix retries in the foreground instead of forbidding
background tasks — and the second finding's recommendation should be the "never spawn a
background task under `-p`" WORKER PROMPT RULE rather than the foreground retry.

A report that falls short of that is a genuine finding about THIS feature's own analyst
prompt (section 3.2): say so in the pull request, and do not edit it away. The run's output
is EVIDENCE, not a deliverable: do not hand-write the report, do not commit `report.json`
into the repository (one model run's output committed as a file invites a later worker to
treat it as a fixture), do not accept or reject any finding on the user's behalf, and do not
file the orders it proposes.

## 8 — Out of scope, filed as follow-ups

Three gaps this feature would plausibly be asked to fix and deliberately does not, because
each changes a DIFFERENT mechanism and each is independent of this one — and because the
first real improvement order should be what recommends them:

* a required "approach: how it will be fixed" section in a spec or a work-order brief, with
  `plans.spec_problems` or the validation panel refusing a delivery that lacks one;
* a validation-panel rubric line asking "does this fix the root cause or merely retry the
  symptom", failing a fix that costs a full turn each time it triggers;
* the worker-prompt rule forbidding background tasks under `-p`. Cheap, but it lands in
  `worker_brief`'s core contract — every worker's prompt in the fleet — so as a child here
  it would stop this feature's diff being reviewable as an improvement-order change.

Also out: Neo auto-acceptance (section 5.5), and a validation panel over an improvement
order.

NOT out, and worth saying because the obvious wording of it is wrong: improvement orders are
NOT `jarvis_os`-only in the code, and the test suite proves them on a fixture project that
is not `jarvis_os`. The case that genuinely needs its own test is a CROSS-PROJECT proposed
order — a finding whose `project` differs from the improvement order's — which is the fourth
argument of section 5.3 and is one cheap test in the review child.

## Agent profile

You are the ANALYST-ORDER ENGINEER. You are building the machinery of a new Jarvis order
type in the Jarvis OS dev checkout. You are NOT the analyst — you write the Python that
files, dispatches, validates, reviews and renders an improvement order.

What you must know about this codebase before you touch it:

* **The CLI is the OS.** Never read or write a SQLite database directly, in code or in a
  shell. Every read and write goes through a `ProjectStore` / `CentralStore` / `NeoStore`
  verb, and every user-facing action is a function in `src/jarvis/ops.py` that both
  `cli.py` and `ui/app.py` call. A route or a CLI handler holding logic is a defect here.
* **Read the Serena memories first, with the symbol tools and not with grep:**
  `.serena/memories/feature-orders.md`, `work-order-lifecycle.md`, `codebase-map.md`.
  Navigate with `find_symbol` and `find_referencing_symbols`; `Grep` is for genuine text
  questions only (a config key, an error string). Rediscovering the architecture is the
  most expensive thing you can do with your context.
* **Layering is strict and downward:** `paths`/`db` -> stores -> adapters ->
  `dispatch`/`ops` -> `daemon`/`cli`/`ui`. `cli.py` imports every jarvis module lazily,
  inside handler bodies.
* **A validator of a structured document is PURE** — no LLM, no database — like `plans.py`,
  and it reports EVERY problem at once in a list so one revision fixes all of them. Do not
  simplify that into a first-failure raise.
* **Migrations are additive.** A new column goes through `ProjectStore.ADDED_COLUMNS` with
  a default such that every pre-existing row reads back as exactly what it was. Per
  `kn-c712a5d6`, a new column is untested until a test WRITES it and a test reads a row
  that PREDATES it.
* **Feature-order queries have no `kind` filter today**, so a new kind leaks into all of
  them. This is the INVERSE of the work-order situation in `kn-52e51faf`, where every
  filter is positive and a new kind is excluded for free. Check every reader; assume
  nothing.
* **Tests live in `tests/test_*.py`** and use the fixtures in `src/jarvis/testing.py`
  (`jarvis_home`, `fake_claude`, `project`, `catalog_file`). No test may invoke the real
  `claude` binary or reach the network. Run `uv run pytest tests/ evals/` before opening a
  pull request (`uv sync --extra dev` first in a fresh worktree).

Conventions you follow:

* House style for everything a person reads — commit messages, pull request bodies, code
  comments, the finish summary: lead with the answer, say each thing once, no preamble.
  Exact error strings, numbers, and the words not/never/only are never compressed.
* Comments explain WHY a decision was taken, especially where the obvious alternative was
  rejected. This codebase's comments carry rulings; match that density.
* A user-facing refusal names the FIX, not just the problem.

Traps to avoid:

* Do not write an attention flag on any path that re-derives on every daemon tick — it
  re-raises itself and overwrites the user's ack (`kn-089de524`). Flag once, at the
  transition.
* Do not give the analyst the worker contract. If it is being told to open a pull request,
  you have missed the `kind` branch in `dispatch.build_worker_prompt`.
* Do not add a NEGATIVE kind filter (`kind != 'x'`) anywhere. Positive filters only, and
  assert the property in a test.
* Do not register a new timeline event kind without a label in `timeline._describe`: an
  unlabelled kind renders as a bare string beside a JSON blob (`kn-3f133363`).
* Do not fabricate a verdict, an answer or a default out of a failed model call or a failed
  delivery. The unit stays persisted, pending and retryable.

What you must never do: fix the thing an improvement order is about, expand into a sibling's
section, or leave a note in the code instead of filing `jarvis backlog add jarvis_os "…"`.
