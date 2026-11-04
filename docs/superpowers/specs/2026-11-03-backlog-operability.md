# Backlog operability — proposals, provenance, filters, promotion with dependencies

Feature order fo-89ba3166. Follows decision order do-722e5c7b: the user decided work
orders MAY keep filing to the internal backlog; this feature gives the user the tools to
operate it. Paths are under `src/jarvis/` unless stated.

## 1. Why, what is fixed, and the shared interfaces

### 1.1 The problem

The backlog (`backlog` table in `os.db`, `CentralStore.add_backlog` and friends) holds
~180 open items. Workers write straight into it with `jarvis backlog add`; nothing reviews
what they file, nothing dedupes it, the dashboard filters only by project and status, and
an item traces back at most to a work order id (`origin_wo_id`) — never to the Neo
question, the worker message, the deferral or the decision proposal that produced it.
Promotion refuses on unfinished dependencies unless `--force`, and then promotes ONE item.

### 1.2 Fixed by the user (do not re-litigate)

1. The internal backlog stays. Workers keep filing deferred work to it (do-722e5c7b).
2. The `/backlog` view filters by project, status, type (bug or task) and severity, like
   the other list views.
3. Every item links back to EXACTLY where it was created: a specific Neo question, a
   specific worker message, a Morpheus order, a panel/validation round, a decision-order
   proposal, a deferral. A work-order id alone is not enough.
4. Workers PROPOSE; they never add. The steward reviews each proposal: it quarantines
   what it is unsure about and escalates that to Neo (Neo may escalate to the user, as
   usual); if it approves, it adds the item, deduped; it decides work order vs feature
   order.
5. Dedupe and dependency-linking must be cheap. No model pass over the whole backlog.
   Where there is no cheap way, the requirement is skipped (see §3.4, §9).
6. Promoting an item promotes all its unsettled dependencies with it, and the dashboard
   shows a confirmation popup listing every item that will be promoted.

### 1.3 Shared interfaces (every section relies on these; §2 creates them)

**Statuses** (`central_store.BACKLOG_STATUSES`, new tuple):
`proposed`, `quarantined`, `open`, `promoted`, `done`, `dropped`.
`SETTLED_BACKLOG_STATUSES = ("done", "dropped")` (central_store.py) is unchanged. A
rejected proposal is `dropped`; a duplicate is `dropped` with `duplicate_of` set. Only
`proposed` and `quarantined` are new values. `open` keeps meaning "in the backlog,
promotable".

**New columns on `backlog`** (additive, through the existing `ADDED_COLUMNS` migration):

| column | type | meaning |
|---|---|---|
| `item_type` | TEXT, `bug`\|`task`, default `task` | what the user filters as "type" |
| `severity` | TEXT, one of `steward.SEVERITIES`, default `medium` | |
| `suggested_kind` | TEXT, `wo`\|`fo`, default `wo` | what promotion creates by default |
| `origin_refs` | TEXT JSON list, default `[]` | §1.3 origin refs, ordered, first = primary |
| `duplicate_of` | TEXT, nullable | the `bl-` id this restates |
| `review_rule` | TEXT, default `''` | the steward rule that decided it (§3) |
| `review_note` | TEXT, default `''` | one human sentence: why |
| `neo_question_id` | INTEGER, nullable | the Neo question deciding a quarantined item (§5) |
| `reviewed_at` | REAL, nullable | |

Existing `origin_wo_id`, `origin_fo_id`, `origin_note` stay and keep being written; they
are what older rows have.

**Origin ref** — a dict, one of these kinds (`central_store.ORIGIN_REF_KINDS`):

| kind | keys | resolves to |
|---|---|---|
| `turn` | `project, wo_id, ts, seq?` | the first `agent_to_user` message of that WO with `ts >=` ref ts: `/wo/<p>/<wo>#msg-<id>`; while none exists yet, `/wo/<p>/<wo>` |
| `message` | `project, wo_id, id` | `/wo/<p>/<wo>#msg-<id>` |
| `neo_question` | `id` | `/neo/question/<id>` |
| `envelope` | `project, id, subject_wo_id` | `/wo/<p>/<subject_wo_id>#envelope-<id>` (the deferral, in the Timeline tab) |
| `decision` | `do_id, key?` | `/go/<do_id>#proposal-<key>`; without a key, `/go/<do_id>` |
| `monitoring_order` | `id` (`mo-`/`mv-`) | `/mo/<id>` |
| `alarm` | `project, id` | `/alarms/<p>/<id>` |
| `validation` | `project, wo_id, round?` | `/wo/<p>/<wo>#round-<round>`; without a round, `/wo/<p>/<wo>#validation` |
| `finding` | `order_id` (`io-`/`inv-`/`do-`), `key` | `/go/<order_id>#finding-<key>`; an `inv-` order has no keyed findings, so `/go/<order_id>#verdict` |
| `gate` | `project, id` | `/gates/<id>?project=<p>` |
| `order` | `id` (any `wo-`/`fo-`) | `/go/<id>` — the weakest ref, used only when nothing finer is known |

**Functions every section calls** (all created in §2):
- `CentralStore.add_backlog(..., item_type="task", severity="medium", suggested_kind="wo",
  origin_refs=None, status="open")` — existing signature, new keyword-only fields.
- `CentralStore.propose_backlog(project, title, description="", *, item_type, severity,
  suggested_kind, origin_refs, depends_on=None, origin_wo_id=None, origin_fo_id=None,
  origin_note="") -> dict` — inserts with `status="proposed"`.
- `CentralStore.review_backlog(item_id, status, *, rule, note, duplicate_of=None,
  suggested_kind=None, depends_on=None, neo_question_id=None) -> dict` — the ONLY writer of
  review outcomes; refuses a transition not in §2.2.
- `ops.origin_url(ref: dict) -> tuple[str, str]` — `(href, label)`; never raises: an
  unresolvable ref returns `("/search?q=<id>", "<kind> <id> (not found)")`.
- `ops.parse_origin_ref(token: str) -> dict` — the CLI spelling of a ref (§4.2).
- `ops.list_backlog` / `ops.show_backlog` payloads gain every new column plus
  `origins: [{kind, href, label}]` (resolved) on each row.

## 2. The record: columns, statuses, origin refs, resolver

### 2.1 Migration

Add the §1.3 columns to `CentralStore`'s `ADDED_COLUMNS` for `backlog`. Existing rows
read `item_type='task'`, `severity='medium'`, `suggested_kind='wo'`, `origin_refs='[]'`.
`get_backlog`/`list_backlog` decode `origin_refs` (`db.from_json`) the way `depends_on` is
decoded. When `origin_refs` is empty but `origin_wo_id` / `origin_fo_id` is set, the
payload synthesises an `order` ref from them, so old rows still navigate as far as they
can.

### 2.2 Status transitions

Allowed by `review_backlog`: `proposed -> open | quarantined | dropped`;
`quarantined -> open | dropped`. Promotion (`open -> promoted`) and settling
(`promoted|open -> done`, `open -> dropped`) keep their existing writers
(`mark_backlog`). `list_backlog(status="open")` semantics are unchanged: proposed and
quarantined items are NOT open and are not promotable — `ops.promote_backlog` already
refuses non-`open` items.

### 2.3 Validation at the store boundary

`item_type`, `severity`, `suggested_kind` and each origin ref's `kind` are validated
against their tuples; unknown keys in a ref are dropped; a ref missing its required keys
raises `ValueError`. This is a trust boundary: refs arrive from worker CLI calls.

### 2.4 Resolver

`ops.origin_url` implements the §1.3 table. The `turn` kind reuses the rule of
`ops.assumption_source_ref` (an assumption is recorded mid-turn and links to the first
reply at or after its timestamp) — `assumption_source_ref(a, replies)` returns a
`message` dict, so factor the "first reply at or after ts" lookup BELOW it and call it
from both; do not copy it. Resolution
is lazy, at render time, because the reply row does not exist while the proposing turn is
still running. `ops.resolve_ref` gains nothing: it already maps `bl-` ids.

### 2.5 Offboarding and search

`ops.quiesce_project` drops `open` items of an offboarded project; it must drop
`proposed` and `quarantined` too. `search.py` indexes the backlog with no status filter;
keep that; hits already carry `status`, so the only change is that a `dropped` row with
`duplicate_of` renders its title suffixed "(duplicate of bl-…)".

## 3. The steward reviews proposals

### 3.1 Where it runs

A new deterministic function `steward.review_proposal(central, item) -> Review` plus a
daemon pass `Daemon.review_backlog_proposals` on the reconcile tick (every tick, all
`proposed` rows, oldest first, at most 50 per tick). No model call. It does NOT go through
`steward.decide`/`steward.gate`: those decide OS self-spend with budgets and a ledger;
a backlog item spends nothing until promoted. Nothing is written to `steward_decisions`
(its ledger and precision readers assume an order behind every row). The outcome lives on
the backlog row: `status`, `review_rule`, `review_note`, `reviewed_at`.

### 3.2 Rules, first match wins

1. **`origin-user`** — the item was not proposed (status already `open`). Not reviewed.
2. **`exact-duplicate`** — another `proposed|quarantined|open|promoted` item in the same
   project has the same normalised title (lowercase, punctuation and whitespace
   collapsed). → `dropped`, `duplicate_of` = that item; the proposal's `origin_refs` are
   APPENDED to the kept item's `origin_refs` (so the kept item shows every place it was
   sighted). If the kept item is `promoted`, the note says which order is already on it.
3. **`unresolved-ref`** — an origin ref resolves to "not found". → `quarantined`.
4. **`possible-duplicate`** — `steward.same_defect`-style word overlap with any
   `proposed|quarantined|open` item in the same project at a STRICTER threshold:
   shared words over the LARGER wording's set ≥ 0.6, both wordings ≥ 3 content words
   (title + first 300 chars of description). → `quarantined`, note names the candidate
   `bl-` ids (at most 3). Never auto-folded: `same_defect`'s 0.4-of-the-smaller rule
   over-merges across a whole backlog, and returns True on an empty wording.
5. **`severity-claim`** — severity `critical` or `blocker`. → `quarantined` (matches the
   OS's existing rule that Neo re-assesses critical/blocker claims).
6. **`kind-mismatch`** — `suggested_kind='wo'` but the description is over 2500
   characters or names more than 6 distinct file paths (`\S+\.(py|html|md|js|toml)`).
   → `quarantined`, note "may be a feature order".
7. **`dangling-dependency`** — a `depends_on` id is `dropped` or missing. → `quarantined`.
8. **`accepted`** → `open`.

### 3.3 Work order vs feature order (user item 4/3.3)

The proposer states `suggested_kind`; rule 6 is the steward's mechanical check of it, and
Neo settles the quarantined cases (§5). `ops.promote_backlog` uses `suggested_kind` when
the caller passes no `--as` (§7).

### 3.4 Dependencies

Only proposer-declared `depends_on` are recorded, validated by rule 7. The steward does
NOT infer dependencies between backlog items: there is no mechanical signal good enough,
and a model pass per proposal over the project's backlog is the token spend the user
ruled out. Neo may add dependencies to a quarantined item it is already deciding (§5).

## 4. Proposing: the CLI, the worker's `add`, the deferral path, the prompts

### 4.1 `jarvis backlog propose`

```
jarvis backlog propose <project> "title" -d "..." --type bug|task --severity <s>
    [--kind wo|fo] [--depends-on bl-…,bl-…] [--ref <token>]... 
```
Calls `CentralStore.propose_backlog`. `--type` and `--severity` are REQUIRED (a proposal
without them cannot be filtered). Inside a work-order session (`JARVIS_WO_ID` and
`JARVIS_PROJECT` set, dispatch.py) a `turn` ref `{project, wo_id, ts=db.now(), seq}` is
prepended automatically, and `origin_wo_id` / `origin_fo_id` (the WO's parent feature) are
filled. Prints the id, `proposed`, and "the steward reviews it within a minute".

### 4.2 `--ref` spelling (`ops.parse_origin_ref`)

`neo:<n>` or `q<n>` → `neo_question`; `msg:<id>` → `message` (current WO);
`mo-…`/`mv-…` → `monitoring_order`; `al-…` → `alarm` (current project);
`do-…[:key]`, `io-…:key`, `inv-…:key` → `decision` / `finding`; `gate-<n>` → `gate`;
`validation[:round]` → `validation` (current WO); `wo-…`/`fo-…` → `order`. Unknown
spelling → error naming the accepted forms. Each ref is checked to exist at propose time
(the id is looked up; a missing one is refused, not stored).

### 4.3 `jarvis backlog add` from a work order becomes a proposal

When `JARVIS_WO_ID` is set, `backlog add` files a PROPOSAL (`--type task --severity
medium` unless given; new optional `--type`/`--severity`/`--kind`/`--ref` on `add` too),
prints that it was proposed, and exits 0. It is converted rather than refused because the
deferral path hands managers a literal `jarvis backlog add … --origin-wo … --origin-fo …
--origin-note …` command (bus.render) and older prompts in flight still say `add`.
Outside a work order, `add` files `open` directly as today: the user typed it.
Existing tests in `tests/test_deferral.py` that expect the router's or a manager's row to
be `open` (and `backlog list` defaulting to `open`) are UPDATED to `proposed`, not
deleted.

### 4.4 The deferral router

`bus._unfilled` (bus.py) files a deferral with no manager: it calls `propose_backlog`
with an `envelope` ref `{project, id, subject_wo_id}` plus an `order` ref for the subject
WO. `ops.file_decision_followups` (`jarvis do follow-up --as backlog`) is user-typed: it
stays a direct `add_backlog` (`open`) and gains a `decision` ref `{do_id, key}`.

### 4.5 The prompts

Every place that tells a worker to run `jarvis backlog add` now says
`jarvis backlog propose <project> "…" -d "…" --type … --severity … [--ref …]`, with one
line: "the steward reviews it; name the Neo question or message that led to it with
`--ref`". Known sites: `dispatch.py` (three: the common briefing ~700, ~1166, ~1264),
`worker_brief.py` (~519, `contract_section`), `ops.py` (~12483),
`assets/skills/report-jarvis-bug/SKILL.md:24`. Find them all with a text search for
`backlog add`. `tests/test_worker_brief.py` pins the contract phrase by phrase: the pin
MOVES to the new invocation, it is not deleted (kn-668ba10e).

## 5. Neo decides quarantined proposals; the user decides what Neo escalates

### 5.1 The question

A new Neo question kind `backlog` (`neo_store.Q_KINDS`). The daemon asks one question per
`quarantined` item with no `neo_question_id` (a pass next to §3.1's), `wo_id=""` (no
worker is waiting), and writes the id back with `review_backlog(...,
neo_question_id=…)` keeping the status `quarantined`. The question body: the item's
title, description (first 1500 chars), type, severity, suggested kind, the steward's
`review_rule` + `review_note`, the titles and ids of the candidate duplicates or
dangling deps it names, and the resolved origin labels. Atomic, no diffs.

### 5.2 The verdict

Neo's verdict keeps its generic shape `{escalate, answer, reason}` (as `morpheus` and the
`validation` kind read it). The question body ENDS with the instruction that `answer`
must be one JSON object `{"decision": "accept"|"reject"|"duplicate", "duplicate_of":
"bl-…"?, "kind": "wo"|"fo"?, "severity": <s>?, "depends_on": ["bl-…"]?}`; the applier
parses `answer` with `structured.parse_json_object` and validates it. `escalate: true`
leaves the item `quarantined` and the question escalated to the user (§5.4, §8). Copy the `kind="validation"` / `kind="triage"` pattern: delivered
first in `Daemon._neo_drain.deliver` with an early return into a module-level
`record_neo_verdict`-style applier that calls `review_backlog`. A `duplicate` verdict
folds `origin_refs` exactly as §3.2 rule 2. An unparseable or failed answer is never
turned into a verdict (kn-40db1828): the item stays `quarantined`, the question retryable.

### 5.3 Every switch site of a new kind (kn-4edb0eb7)

A `Q_KIND` is a claim about who is waiting; `backlog` means nobody is. All of these must
learn it, or questions strand or message a worker that does not exist:
`neo_store.Q_KINDS`; `neo_store.REVIVABLE_KINDS`; the deliver branch in
`Daemon._neo_drain`; `Daemon._note_question_unreachable`; the `refused` handling and the
cleanup exclusion tuple near it in `_neo_drain`; `ops.neo_answer_escalated` (must NOT
forward to a worker — it applies the user's answer or points at `jarvis backlog review`);
`ops.neo_review`'s `--correct` tail (must not message anyone); the `answer_form` macro in
`ui/templates/_question.html`; `ops._neo_attention`; and
`invariants.check_neo_escalations_are_live` with a `_stale_backlog_question` predicate
(stale once the item is no longer `quarantined`). The question is closed only through
`backlog.neo_question_id` (kn-106106a1).

### 5.4 The user's verb

`jarvis backlog review <bl-id> --accept|--reject|--duplicate-of <bl-id> [--kind wo|fo]
[--feedback "why"]`, and `ops.review_backlog_item(...)` behind it (the UI in §8 calls the
same function). Works on any `quarantined` item, escalated or not; records the user's
reason as a Neo learning when `--feedback` is given (the `neo review --correct` path,
without the worker forward); closes the Neo question. A worker session (`JARVIS_WO_ID`
set) is refused in `ops.review_backlog_item` before anything changes.

## 6. The backlog list and item page: filters and navigation

### 6.1 Filters

`create_app/backlog` (ui/app.py) adds filter options `item_type`, `severity`,
`suggested_kind` next to `status` and `project`, through the existing
`filter_options` / `query_filters` / `filter_rows` helpers and the `ui.filter_bar` macro.
Severity options sort by `steward.severity_rank`, not alphabetically. Default view
(no status filter) shows every status except `dropped` and `done`; a status filter shows
exactly that status. The page head counts open, proposed, quarantined. Existing
`tests/test_ui.py` backlog assertions that expect settled rows in the default view are
updated to pass `?status=…`.

### 6.2 Columns

Add `type`, `severity`, `kind`, and an `origin` column: the primary origin as a link
(`origins[0]`), plus "+N" when there are more. Keep `became` (promoted order). A
`dropped` duplicate shows "duplicate of bl-…" linking to the kept item.

### 6.3 Item page (`backlog_item.html`)

An **Origin** panel lists every resolved origin (kind, label, link) in order, then the
chain upward: the origin WO's parent feature order (`origin_fo_id`) if any. A **Review**
line shows `review_rule`, `review_note`, `reviewed_at`, and the Neo question link when
`neo_question_id` is set. Duplicates folded INTO this item are listed (rows whose
`duplicate_of` is this id). CLI `jarvis backlog show` prints the same origins as
`kind label href` lines; `jarvis backlog list` gains `--status`, `--type`,
`--severity`, `--kind` filters.

## 7. Promote with dependencies

### 7.1 The closure

`ops.promote_backlog(item_id, *, as_feature=None, dry_run=False, ...)` computes the
transitive closure of the item's `depends_on` whose members are `open` (unsettled,
unpromoted). `promoted` members are not re-promoted. `dropped`/missing members, or a
`proposed`/`quarantined` member, refuse the whole promotion with their ids (nothing is
created). `dry_run=True` returns the plan — `[{id, title, project, kind, waits_on: [...]}]`
in dependency order — and creates nothing; the CLI and the UI popup both read it.

### 7.2 Creating the orders

Members are promoted in dependency order. Each member's kind is `suggested_kind`; the
root's kind is overridden by `--as` when given. A WO member gets
`blocked_by=["bl-<dep>" for each unsettled direct dependency]` — NOT `depends_on`: the
daemon clears a `bl-` hold once the item reaches `done`/`dropped` (daemon.py ~7725,
`preconditions.checked_ref`), which works across projects and for deps promoted as
feature orders, and a promoted dep stays unsettled until its order closes it. An FO
member with unsettled dependencies is REFUSED up front (`create_feature_order` has no
`blocked_by`, so it would start planning at once); the error names it and says to promote
its dependencies first. `--force` is removed from the dependency check (deps are now
promoted, not skipped); keep the flag accepted for one release as a no-op with a
deprecation line. Trap: when `blocked_by` is passed, `create_work_order` skips its own
`backlog_hold` auto-detection (ops.py ~944), so a member would lose that hold — compute
it yourself and pass it in the same `blocked_by` list.

### 7.3 CLI and popup

`jarvis backlog promote <id>` prints the closure and requires `--yes` when it has more
than one member (exit 1 with the list otherwise). The dashboard's Promote buttons (list
and item page) open a native `<dialog>` (no `alert`/`confirm`) listing every member from
the dry run — id, title, kind, project — with Confirm and Cancel; Confirm posts to
`/backlog/promote/<id>` with `confirm=1`. The dialog lives in its own include
`ui/templates/_promote_dialog.html` and is fed by a small JSON route
`GET /backlog/<id>/promote-plan`. Single-member promotions skip the dialog. The server
REFUSES a multi-member POST without `confirm=1` (redirect back with a flash naming the
members), so the popup is not client-only. `promote_backlog`'s `as_feature` becomes
`bool | None` (None = use `suggested_kind`); update the UI caller. Existing tests
`tests/test_pipeline.py::test_backlog_promotion_with_dependencies` and
`tests/test_ui.py::test_backlog_page_promote_blocked_then_forced` are REWRITTEN to the new
behaviour, not deleted; `tests/test_preconditions.py` must stay green.

## 8. Quarantine on the dashboard and in the pulse

`/backlog` gets a **Needs you** block (the `ui.needs_you` macro) listing `quarantined`
items whose Neo question is ESCALATED to the user, with Accept / Reject buttons posting to
`/backlog/review/<id>` (calls `ops.review_backlog_item`), and a second, collapsed list of
quarantined items Neo is still deciding. `ops.os_status` (and `jarvis status
--attention`) gains a `backlog_escalated` count with the `jarvis backlog review` command;
the project page (ui/app.py ~1425) shows proposed / quarantined counts beside open.

## 9. Out of scope, rejected alternatives

- Inferring dependencies between backlog items (user item 3.2's second half): skipped, no
  cheap mechanism (§3.4). Filed to the backlog as a follow-up.
- Making Morpheus, the panel or investigations PROPOSE items themselves: they do not file
  backlog items today (panel follow-ups go to GitHub issues, pinned by
  `tests/test_validation_follow_ups.py::test_nothing_reaches_the_projects_backlog_any_more`).
  A worker acting on them names them with `--ref`.
- Triage of the ~180 existing open items: they stay `open`, `task`/`medium`.
- Running proposals through `steward.decide`/`steward_decisions`: rejected, §3.1.
- Auto-folding fuzzy duplicates: rejected, §3.2 rule 4.

## Agent profile

You are a Jarvis OS developer implementing ONE section of the backlog-operability feature
in the dev checkout of `agentic_os` (Python 3.11+, stdlib-only core: argparse + sqlite3 +
json; a FastAPI/Jinja dashboard under `src/jarvis/ui/`). You build your section of
`docs/superpowers/specs/2026-11-03-backlog-operability.md` and nothing else. Section 1.3
is the contract every sibling relies on: never rename its statuses, columns, ref kinds or
functions; if your section cannot be built against it, ask Neo (`jarvis wo ask`) instead
of bending it.

What you must know about this codebase:
* Read the Serena memories first (`codebase-map`, `testing`, and `neo-panel` if you touch
  Neo). Navigate with Serena's symbol tools (`find_symbol`, `find_referencing_symbols`,
  `get_symbols_overview`); the hooks refuse grep/sed over Python. Line numbers in the spec
  are approximate; symbol names are exact.
* The backlog lives in `os.db` behind `CentralStore` (`central_store.py`). No module opens
  SQLite directly; schema changes go through `ADDED_COLUMNS`. Never poke a database by
  hand.
* The CLI imports lazily inside handlers; UI POSTs delegate to `ops`; the UI never writes
  a store directly.
* `list_backlog(project, status='open')`'s default is a FILTER (kn-05c1c1d1): a dedupe or
  count that means "everything" must pass `status=None`.
* The steward (`steward.py`) is deterministic and makes no model call. Keep it that way.
* Worker sessions carry `JARVIS_WO_ID` and `JARVIS_PROJECT` (dispatch.py).

Rules you must follow:
* Never fabricate a verdict or default from a failed model call (kn-40db1828): the unit
  stays pending and retryable.
* When you add a status, kind or column, text-search `tests/` for the literal values and
  the tuple names: exact-tuple pins live in files you did not touch.
* When you replace a command a worker is told to run, move its pin in
  `tests/test_worker_brief.py`; never just delete it.
* Write a test for the vacuous version of each acceptance check (one that would pass for
  the wrong reason) and make sure yours does not; an "empty messages" assertion needs a
  paired positive.
* Run only the targeted tests for what you changed (`uv run pytest tests/<file> -q`);
  never the full suite locally — CI runs it.
* No `alert()`/`confirm()` in the dashboard; use `<dialog>`. The dashboard loads no CDN.
* Do not quote shell commands verbatim inside `--description`, `--why` or commit
  messages (the gate recogniser matches text); write prose to a file and pass the path
  where the verb supports it.
* The PR body needs a `## How to validate in production` section.
* File deferred work with `jarvis backlog propose` once §4 has landed, `jarvis backlog
  add` before; never leave TODO notes.

What you must never do: touch a sibling's section beyond the interface in §1.3; change
`steward.decide`/`gate` or the steward ledger; send a message to a worker from the
`backlog` Neo kind; run the full test suite; edit anything under
`~/workspace/production`.
