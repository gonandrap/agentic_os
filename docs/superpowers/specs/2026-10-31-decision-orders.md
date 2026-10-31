# Decision orders — an order kind that gathers data and waits for the user

A decision order (`jarvis do …`, id prefix `do-`) is an order whose deliverable is a
report the USER decides on. A researcher reads, measures and argues options; the report
lands on the record; the user is told; the order then waits for the user for as long as
it takes. Nothing in the OS completes one. Only `jarvis do done` (or `jarvis do cancel`)
settles it.

## 1. Why, what is fixed, and the shared interfaces

### 1.1 The two cases

* **wo-5e58ef32** (painforwisdom). The description said "after reviewing and approving
  that plan, I'll proceed". The worker wrote the plan (PR 90, docs only), the panel passed
  it, Neo approved `auto_merge` (gate 14, Q2707, "the user's review-and-approve step
  concerns the plan's execution"), `ops.complete_merged` closed the order. The only inbox
  item was INFO 2288 "Neo approved auto_merge".
* **wo-d83e989e** (jarvis_os, a question). The final message listed 2 gaps and offered to
  file them; the panel logged "Review filed no follow-ups"; the order completed 11 s after
  an attention flag; zero inbox items.

Both were asks to gather data for a decision, filed as work orders, and the work-order
lifecycle is built to finish on its own. Issue 1385 files the general complaint.

### 1.2 Fixed by the user (do not re-litigate)

1. A NEW kind, tracked as its own kind on every surface. Regular work-order prompts and
   behaviour stay unchanged.
2. A decision order NEVER completes automatically. Only the user completes one.
3. Validation checks only that no code changed and that the deliverable is data/analysis
   answering the ask. No code-quality review.
4. Neo may answer the researcher's questions as usual; it never decides on the user's
   behalf and never approves anything that closes the order.

### 1.3 Shared interfaces (every section below relies on these; none owns them alone)

**Record.** A `feature_orders` row, `kind='decision'`, id prefix `do`, plus exactly one
child work order `kind='researcher'` linked through `feature_orders.plan_wo_id` and
`work_orders.parent_id` — the investigation shape (§2.1 of
`docs/superpowers/specs/2026-09-27-investigation-orders.md`).

**Statuses** (members of the existing `FO_STATUSES` plus ONE new one):

| status | label | meaning |
|---|---|---|
| `pending` | pending | created, researcher not opened yet |
| `planning` | researching | researcher running, or its report under validation |
| `awaiting_decision` (NEW) | awaiting your decision | report delivered; the user owes a decision |
| `budget_exhausted` | budget exhausted | family budget gone (existing machinery) |
| `completed` | decided | the user ran `jarvis do done` |
| `cancelled` | cancelled | the user ran `jarvis do cancel` |

A decision order never enters `plan_review`, `executing`, `validating` or `failed`. So
`settle_features`, feature-level validation and Neo's plan review — which scan those —
never see it. A researcher that dies does NOT fail the order (§5.3): the only terminal
transitions are the user's two verbs.

**The report** — the JSON document the researcher submits with
`jarvis do report <do-id> --from-file report.json`, stored WHOLE in
`feature_orders.plan` (replacing any earlier one):

```json
{
  "summary": "one paragraph: the answer to the ask, numbers first",
  "findings": [{"key": "f1", "title": "...", "detail": "...", "evidence": ["..."]}],
  "options": [{"key": "a", "title": "...", "pros": ["..."], "cons": ["..."], "cost": "..."}],
  "recommendation": {"option": "a", "why": "..."},
  "proposed_orders": [{"key": "p1", "as": "wo|fo|backlog", "project": "", "title": "...",
                       "description": "..."}]
}
```

`summary`, `findings` (≥1) and `recommendation` are required; `options` and
`proposed_orders` may be empty. `evidence` items are free text: a command and its output,
a record id, a file:line, a URL. Keys are unique slugs within their list.
`proposed_orders[].project` empty means the decision order's own project.

**Events** (on the RESEARCHER's timeline, as investigations do — `ops.feature_event` is
manager-only): `decision_report_submitted` {do_id, sha} (§3), `decision_delivered`
{do_id, sha, revised: bool} (§5), `decision_followup_filed` {do_id, key, order_id} (§6),
`decision_closed_by_user` {do_id, status, note} (§2). Each needs a label in
`timeline._describe` (`kn-3f133363`). `sha` is `project_store.decision_report_sha(plan)`
(§2): sha256 of the report JSON with every `proposed_orders[].filed` key removed and keys
sorted — so filing a follow-up (§6) never reads as a revised report (§5).

**Commands:** `jarvis do create|list|show|done|cancel|budget` (§2), `jarvis do report`
(§3), `jarvis do send` (§5), `jarvis do follow-up` (§6).

### 1.4 Decided in this spec: no committed report, no branch, no pull request

The report lives on the record (`feature_orders.plan`), not in the repository. The
researcher never commits, never pushes, never opens a pull request. Therefore there is no
PR for `Daemon.poll_pull_requests` to watch, no `pr_merge` gate for Neo to approve, and
no `ops.complete_merged` path — wo-5e58ef32's failure is impossible by construction
rather than by a rule. A user who wants the report as a document files a follow-up
(`--as wo`) that writes it. Scratch files the research needs go under the worktree's
`.jarvis/scratch/` (gitignored) or outside the worktree.

## 2. The kind, its states and the user-only close

Owns the record. Every later section builds on it.

**Constants** (`src/jarvis/project_store.py`), each beside its `investigation` sibling,
per kn-97fc3024's list:

* `FO_KINDS` += `"decision"`; `FO_ID_PREFIXES["decision"] = "do"`.
* `FO_STATUSES` += `"awaiting_decision"` (after `validating`), and the same in
  `FO_OPEN_STATUSES`.
* `FO_STATUS_LABELS["decision"] = {"planning": "researching", "awaiting_decision":
  "awaiting your decision", "completed": "decided"}`.
* `FO_DECIDE_COMMANDS["decision"] = "jarvis do show {fo_id}"`.
* `is_feature_order_id` learns `"do-"` (no collision with `fo-`/`io-`/`inv-` under
  `startswith`).
* `WO_KINDS` += `"researcher"`; `WO_KIND_CONTRACTS["researcher"] =
  KindContract(SETTLE_BY_FINISH, artifact="report.json",
  submit="jarvis do report {fo_id} --from-file report.json", lands_code=False)`.
  SETTLE_BY_FINISH, not in-session: `ops.exempt_from_validation` exempts in-session kinds
  from the panel, and §4 needs a round. Finish also brings `ops.unlanded_refusal`, which
  refuses a `lands_code=False` worktree holding anything but its artifact — the
  mechanical "no code changed" check, already written.
* `FAMILY_CHILD_KINDS` (project_store.py:~336, holding
  `"investigation": ("investigator",)`) gains `"decision": ("researcher",)`.
* The new status needs its render vocabulary in the same change, or the render tests go
  red: `ui/app.py` `FO_STATUS_META` and `cli.py` `FO_ICON`, and the FO_STATUSES-ordered
  status sort in `ops.py` (~1657). `test_a_validating_feature_order_renders_everywhere_it_appears`
  is the model for a new pinning test.
* `decision_report_sha(plan: str | dict | None) -> str` — the §1.3 hash, a pure helper
  beside the constants ("" for an empty plan), so §3, §5 and §6 share one definition.
* `ops._require_kind`'s phrase table gains `decision` -> "a decision order".
* `Daemon._FAMILY_RUNNING_STATUS` (daemon.py:~152) gains `"decision": "planning"`, so the
  family-budget pass can escalate a researching order but never touches one in
  `awaiting_decision` (the user is not spending money by thinking).

**The guard — the never-auto-complete rule in code.** `ProjectStore.update_feature_order`
(the one writer; `set_feature_status` calls it, and no raw SQL writes FO status) refuses
to write a status in `FO_TERMINAL_STATUSES` onto a `kind='decision'` row unless the caller
passes `decided_by_user=True`; it raises `DecisionOrderGuard` (a `RuntimeError` subclass)
naming the row and the attempted status. `set_feature_status` forwards the keyword. Only
the two ops functions below pass it. A grep-able pin test asserts no other
`decided_by_user=True` exists in `src/`.

**ops** (each beside its `investigation` twin, CLI a thin wrapper):

```python
def create_decision_order(project_name, title, description, budget_usd=None,
                          origin="jarvis") -> dict      # description REQUIRED: the researcher sees only it
def list_decision_orders(project_name=None, include_settled=False) -> list[dict]
def show_decision_order(do_id, project_name=None) -> dict   # row + researcher {id,status,result_summary} + report (raw dict)
def complete_decision_order(do_id, note="", project_name=None) -> dict   # `jarvis do done`
def cancel_decision_order(do_id, project_name=None) -> dict              # `jarvis do cancel`
```

* `complete_decision_order` and `cancel_decision_order` REFUSE when `JARVIS_WO_ID` is set
  in the environment (any worker, planner, researcher or Neo-driven session) — the
  `ops.close_out` precedent. The refusal says it is the user's verb. They write
  `decision_closed_by_user` on the researcher's timeline (or the order's own events when no
  researcher exists), clear the attention flag, and stop a live researcher
  (`cancel_feature_order`'s machinery, which is kind-agnostic). `done` is refused from
  `pending`/`planning` only if no report is stored, with a message pointing at
  `jarvis do cancel`; from `budget_exhausted` it is allowed.
* `ops.create_work_order` refuses a `parent_id` naming a non-`feature` order unless the
  child kind is that order's own child kind (the daemon opening the researcher). Today
  `--parent do-…` would be accepted because `awaiting_decision` is open.
* `jarvis do budget <id> [<usd>] [--clear]` is `fo budget`'s function with `_require_kind`.
* The generic feature-order verbs (`fo cancel`, `fo resume`, `fo approve`, the dashboard
  `/fo/` cancel route) given a `do-` id refuse cleanly via `_require_kind`, naming the
  `jarvis do` verb — never a `DecisionOrderGuard` traceback.

**CLI** (`cli.py`): a `do` subparser with `create <project> "title" -d "..." [--budget]`,
`list [project] [--all]`, `show <id> [--json]`, `done <id> [--note "..."]`,
`cancel <id>`, `budget`. `show` prints the report's `summary`, `recommendation` and key
lists plainly; §3 replaces that with the full renderer.

**Tests:** the guard (every terminal status refused without the keyword, allowed with it,
feature/improvement/investigation rows unaffected); the env refusal; tuple pins in
`tests/test_stores.py` updated (grep `tests/` for the tuple names AND for the literal
strings `"decision"`/`"researcher"` used as an unknown-kind example, per kn-97fc3024);
`check_feature_failures_are_real` leaves a decision row alone (it is safe only because
`feature_children(kinds=WORKER_CHILDREN)` excludes the researcher — pin it).

## 3. The researcher: brief, report, submission

Owns what the researcher is told, what it may run, and the report it hands back.

**Opening it.** A sibling loop in `Daemon.plan_features` beside the investigation one:
`list_feature_orders(statuses=("pending",), kind="decision")`, create one
`kind="researcher"` child (`origin="os"`, `parent_id=fo["id"]`, description = the order's
description verbatim), then `update_feature_order(plan_wo_id=…)` and
`set_feature_status(…, "planning")` LAST, so a crashed tick re-files rather than strands.

**The kind branches** (kn-31f0f450: a new kind falls to `else` and gets the worker
contract — told to open a PR):

* `dispatch.build_worker_prompt` -> `_researcher_prompt(wo, project, knowledge, feature)`
  beside `_analyst_prompt`.
* `dispatch.READ_ONLY_KINDS` and the kind lists at dispatch.py:~378/~385 gain
  `researcher`.
* `supervisor._what_it_is` gains a researcher branch: a read-and-measure session that
  ends by submitting a report; long reads and many commands are normal; a commit or a PR
  is NOT.

**The brief** (`_researcher_prompt`) says, in this order: you are gathering data for a
decision the USER will take; the ask (the description verbatim); read-only — run any
read command, query or analysis script, write scratch files only under
`.jarvis/scratch/` or outside the worktree, never edit tracked files, never commit, push
or open a pull request; deliver the §1.3 report with `jarvis do report <do-id> --from-file
report.json` — that IS your finish, then end your turn; present options and a
recommendation, never act on one; proposed follow-up orders are proposals the user may
file, never things you file; questions go to Neo with `jarvis wo ask` as usual, but WHICH
OPTION TO TAKE is the user's question — say so in the report, never ask Neo to choose. It
embeds the report schema from `decision_report.SCHEMA_TEXT` so brief and parser cannot
drift.

**No-write enforcement — a narrow hook, not the investigator lockdown.** The
investigator's allow-list (hooks.py:~1129-1176) would stop a researcher running analysis
scripts, which is the job. Instead, for `JARVIS_WO_KIND=researcher`, a PreToolUse branch
in `hooks.py` refuses: `git commit`, `git push`, `gh pr create`, `gh pr merge`, and
Write/Edit/NotebookEdit to a path inside the worktree other than `report.json` and
`.jarvis/scratch/**`. Each refusal names the allowed alternative. `unlanded_refusal` at
finish is the backstop. Test that every command a refusal suggests is accepted by the same
hook (kn-30e15974 item 2).

**Neo.** `neo.build_system_prompt` (or the question context it renders) gains one rule,
applied when the asking work order's kind is `researcher`: a question asking which option
to take, whether to proceed, or to approve anything on the user's behalf is escalated to
the user, never answered. Nothing else about Neo changes: the report is NOT put to Neo for
review (do not reuse `ops.findings_reviewed_by_neo`/`_ask_findings_review`).

**The report module** `src/jarvis/decision_report.py` — pure, no LLM, no database, the
`findings.py` shape: `parse_report(doc) -> dict` raising `DecisionReportError(problems:
list[str])` with EVERY problem at once; `SCHEMA_TEXT`; `render_report(report) -> str`
(plain text for `jarvis do show` and the inbox body); `headline(fo, report) -> str` (one
line: counts + the recommended option's title, used by §5's attention reason). Caps like
`findings.MAX_*` (summary ≤ 1500 chars, ≤ 20 findings, ≤ 10 options, ≤ 10 proposed
orders).

**Submission** `ops.submit_decision_report(do_id, doc, project_name=None)`: `_require_kind`
decision; accepted while `planning` or `awaiting_decision` (a revision after the user
asked for more, §5); `parse_report`; store with `update_feature_order(plan=…)`; write
`decision_report_submitted` {do_id, sha}; then `_settle_submitter(path, plan_wo_id,
summary, "decision_report_settled", …)` — which runs `finish` and opens §4's round. It
does NOT change the order's status and raises no flag: delivery is §5's, after
validation. `jarvis do report <id> --from-file <path|->` wraps it. `jarvis do show` and
the CLI switch to `render_report`.

## 4. Validation: a narrow round over a report

Owns how a researcher's finish is judged. Two pieces.

**4.1 The void trap.** `ops._plan_effects` (ops.py:~14372) emits an attested
`plan_submitted` effect for ANY feature order whose `plan_wo_id` is this work order and
whose `plan` is non-empty. A researcher's report would trigger it; with no files changed,
`evidence.nothing_to_judge` (evidence.py:~231) returns `"void"`, no seat is asked, and the
round passes unexamined. Fix: `_plan_effects` skips `kind='decision'` rows (positive
filter: emit `plan_submitted` only for kinds that have it today), and a new UNATTESTED
`decision_report` effect carries the stored report (rendered by
`decision_report.render_report`, or the raw JSON if that module is absent) plus the
order's description, so the round has something to judge and seats are asked.

**4.2 A per-kind roster.** There is no per-kind roster today: `Daemon._validator`
(daemon.py:~2651) passes the project's `cfg.roster` to `validation.decide`. Add
`roster: tuple[str, ...] | None = None` to `KindContract`; at that call site, when the
work order's contract has one, use `dataclasses.replace(cfg, roster=contract.roster)`.
Set `WO_KIND_CONTRACTS["researcher"].roster = ("deliverable", "chair")`.

**The `deliverable` seat**: `VALIDATOR_SEATS` += `"deliverable"` — but
`catalog.DEFAULT_VALIDATION_ROSTER` (catalog.py:~272) is `VALIDATOR_SEATS` today, which
would put the seat on every regular work order's panel and break §1.2 item 1. Keep the
default roster to the current five seats (e.g. a `KIND_ONLY_SEATS = ("deliverable",)`
excluded from it, and refused in a project's configured roster), and pin that a regular
work order's roster is unchanged. Asset
`src/jarvis/assets/validator-seats/deliverable.md` in the existing seat format. Its
mandate, and nothing more: (1) the diff is empty — any changed tracked file is a BLOCKER
(the finish refusal should have caught it; the seat states it if not); (2) the report
answers the description's ask, with evidence behind its findings — a recommendation with
no data behind it, or a report about a different question, is a BLOCKER; (3) everything
else (style, depth, extra options) is a FOLLOW-UP at most, per kn-fd51ff0b. It never
reviews code quality and never judges whether the recommendation is the right choice —
that is the user's decision. Not a veto seat.

The narrow roster applies whatever the project's validation roster is; when validation is
disabled for the project, no round opens and the researcher settles `completed` at
finish, which is what §5 keys on.

**Tests:** a researcher's finish over a stored report opens a round with seats
`deliverable`+`chair` (not void, not the project roster); a feature planner's
`plan_submitted` effect is unchanged; a dirty tracked file still refuses at finish.

## 5. Delivery and doctor: from a validated report to the user, and the post-conditions that hold it

Owns the transition to `awaiting_decision`, the notification, the attention line, the
`send` verb, what happens when the researcher dies, and the doctor post-conditions.

**5.1 The delivery pass** `Daemon.deliver_decisions(project, store)` on every reconcile
tick, positive filter `kind="decision"`, statuses `planning` and `awaiting_decision`:

* Researcher `completed`, `plan` non-empty, and `decision_report_sha(plan)` differs from the `sha` of the
  latest `decision_delivered` event (none = first delivery): in ONE call set
  `awaiting_decision` (if not already), `flag_feature_attention(do_id, "decision awaiting
  you: " + decision_report.headline(...) + " — jarvis do show <id>")`,
  `store.add_notification(level="warning", source="decision", title=f"{do_id} report
  ready: {title}" (or "revised"), body=<summary + recommendation, rendered>)` — the same
  path `budget.escalate_feature` uses to reach the user's sinks, Telegram included;
  verify that path reaches `notify.route_new_inbox` in a test — and write
  `decision_delivered` {sha, revised}. Delivery-once is BY CONSTRUCTION: the sha
  comparison, not a flag read (kn-089de524).
* Nothing on this pass ever lowers the flag or writes a terminal status. The flag comes
  down only in §2's `done`/`cancel`.

**5.2 `jarvis do send <id> "msg"`** — `ops.send_decision_message`: `ops.send_message` to the
researcher (it revives a completed session). The order STAYS `awaiting_decision` and the
flag stays up: the user still owes the decision. If the researcher resubmits, §3 stores
the new report, §4 judges it, and §5.1 delivers it as `revised`. Refused on a terminal
order.

**5.3 The researcher dies.** Researcher `failed` or `cancelled` while the order is
`planning`: the order stays `planning` (§1.3 — the OS writes no terminal status) and
§5.1's pass raises the attention flag once (only when not already flagged) with reason
"researcher <wo-id> <status> — jarvis do send <do-id> \"retry\" or jarvis do cancel
<do-id>", plus one `warning` notification. A `budget_exhausted` order is the existing
family-budget machinery; raising the budget resumes it.

**5.4 Status and attention.** `ops.os_status`'s feature rollup (ops.py:~611) is
kind-agnostic and will carry the row; give it the `decide` command from
`FO_DECIDE_COMMANDS` and widen the `improvement` check at ops.py:~640 so a decision
order's reason is not prefixed by `feature_progress`. `jarvis status --attention` must
list an `awaiting_decision` order on every tick until done/cancel —
`invariants.check_settled_parents_have_no_orphans` (invariants.py:~2628) touches flags on
any kind; cover it.

**5.5 Doctor post-conditions** (`invariants.INVARIANTS`). The ask's "doctor must know
the kind" is owned HERE: `jarvis doctor` runs these, and §7 adds none:

* `INV-DECISION-USER-CLOSED` — a `kind='decision'` row in a terminal status with no
  `decision_closed_by_user` event. Never repairable; reports critical. This is the
  detector for the guard ever being bypassed.
* `INV-DECISION-AWAITING-FLAGGED` — an `awaiting_decision` row with no attention flag.
  Repairs by re-raising §5.1's reason (there is no ack for a feature-level flag, so this
  cannot fight the user).

**5.6 The never-auto-complete suite** (`tests/test_decision_orders.py`): drive a decision
order to `awaiting_decision`, then run `daemon.tick()` many times with every pass that
could plausibly settle an order active — PR poll, settle_features, invariants with
repair, family budget, stuck sweep, fleet health, landed-investigator retirement — and
assert status stays `awaiting_decision`, flag stays up, exactly one notification exists;
then `complete_decision_order` and assert `completed` + flag down + event. Plus: a
researcher dying leaves the order open and flagged; a revision notifies once; and the
non-daemon routes leave the ORDER open — `jarvis wo done` / `ops.close_out` on the
researcher, `fo budget` raise and clear on the do row, `wo hide`/`wo delete` of the
researcher (each then handled by §5.3 like a dead researcher).

## 6. Follow-ups from the recommendation

Owns turning a report's `proposed_orders` into real orders. Never closes the decision
order — filing work and deciding are two decisions (kn-a5e02565).

`jarvis do follow-up <do-id> <key>... [--all] [--as wo|fo|backlog]` ->
`ops.file_decision_followups(do_id, keys, as_=None, project_name=None)`. Per key, the
proposed order's own `as` unless `--as` overrides: `create_work_order`,
`create_feature_order`, or `CentralStore.add_backlog`, in the proposal's `project` (empty
= the order's own). The description's FIRST LINE names the origin ("Filed from decision
order <do-id> — run `jarvis do show <do-id>` for the data behind it."), then the
proposal's description. `metadata[ORIGIN_DO_KEY] = do_id` (new constant beside
`ORIGIN_IO_KEY`), `parent_id` NULL — a proposal is not a child (§5.3 of
`docs/superpowers/specs/2026-09-23-improvement-orders.md`).

Record what was filed in the stored report: `proposed_orders[i]["filed"] = [{"id",
"as", "at"}]`, written back with `update_feature_order(plan=…)`; `decision_report_sha` ignores `filed`,
so §5.1 does not re-deliver it as a revision. A key already filed is refused unless
`--again`. A failed filing reports the error and files nothing further for that key; the
decision order is untouched either way. Event `decision_followup_filed` per order.
Allowed in `awaiting_decision` and after `completed` (the user may decide first and file
later); refused while `planning` (no validated report yet).

The back-link is readable from both ends: `show_decision_order` lists filed orders; the
filed order's `jarvis wo show` names `origin_do`.

## 7. Surfaces: dashboard, search, cost, inspect, and the operator docs

Owns everything the user and Jarvis READ about the kind, beyond the CLI verbs.

* **Dashboard** (`ui/app.py`, templates): `/do/<project>/<do-id>` (the segment comes free
  from `ORDER_SEGMENTS`) renders the order like the `/inv/` and `/io/` pages: status
  label, description, the rendered report (summary, findings with evidence, options,
  recommendation), proposed orders with a "File" button per key (POST ->
  `ops.file_decision_followups`), "Done" (with optional note) and "Cancel" buttons (POST ->
  `ops.complete_decision_order` / `cancel_decision_order`), a send box (POST ->
  `ops.send_decision_message`), and the researcher link. Every POST delegates to ops. The
  project page and `/api/status` list decision orders as their own group; extend
  `test_api_status_carries_every_feature_order_kind`.
* **Search** (`search.py`): `jarvis search --kind decision` finds them; the report's
  summary and recommendation are indexed text.
* **Cost / inspect**: `jarvis cost <do-id>` and `jarvis inspect <do-id>` resolve
  (`is_feature_order_id` already learns `do-` in §2); the family is the order plus its
  researcher, and the bill shows it. Add tests; fix any kind enumeration in `bill.py`,
  `inspection.py` or `fleetcost.py` that lists FO kinds explicitly.
* **Notify links**: `notify.check_wo_link` resolves a `do-` id to its page.
* **CLAUDE.md crib sheet**: one block after the `jarvis investigate` lines:
  `jarvis do create <project> "title" -d "<the decision and what data would settle it>"`
  `# "do" = decision order: data for YOUR decision; never completes on its own`, then one
  line `jarvis do show|send|follow-up|done|cancel <id>`. Keep it shorter than the
  investigate block and move the slice boundaries in `tests/test_persona.py` so its
  existing length pins still slice what they meant to; add a pin for the do block.
  Routing rule in the crib's prose: an ask whose deliverable is information for the
  user's own decision ("find out…", "compare…", "gather data so I can decide") goes to
  `jarvis do create`, never `jarvis wo create`.
* **Operator docs**: `docs/os/operator/orders.md` gains a `## jarvis do …` entry per crib
  line, the routing rule, and the never-auto-complete promise; `docs/os/index.md` and
  `docs/os/architecture/feature-orders.md` name the kind.
* **Evals**: add one case to `evals/llm/test_jarvis_judgment.py` that an ask like "gather
  the cost data so I can decide whether to drop the panel" routes to `jarvis do create`.

## 8. Where this covers issue 1385, and what stays out

Covered for decision-shaped asks: "a description saying the user decides blocks
auto-merge" (no PR exists, §1.4); "completion reaches the inbox with its result" (§5.1
delivers; only the user completes); "open items are filed or the order stops" (proposals
are on the record and filed by §6, the order waits). Not covered, and staying with issue
1385: silent completion of ordinary user-created work orders, final-message offers on
regular orders, and Jarvis routing a decision-shaped ask to `wo create` anyway (§7's crib
rule is the only mitigation). No change to regular work-order prompts or lifecycle.

Also out: a per-project `worker.decision_model` / effort knob (researchers run on worker
defaults); auto-routing from `wo create` into `do create`; Neo reviewing a report.

## 9. Rejected alternatives

* **Reuse `plan_review` as the waiting state (the io precedent).** `plan_review` means
  "Neo is reviewing" on every surface (`FO_STATUS_META`, `FO_ICON`), and Neo's plan-verdict
  path keys on it. A dedicated status makes "only the user leaves it" a property you can
  grep for.
* **A `work_orders` row of a new kind, no parent.** The work-order lifecycle is the one
  that completes on its own (settle_work_order, PR poller, close_out); every one of those
  paths would need a carve-out. The feature-order row has none of them and already has
  budget families, a child link and kind-aware labels.
* **Commit the report as a doc that never auto-merges.** Keeps a PR in the loop, so the
  poller, the `pr_merge` gate and `complete_merged` all need decision-order exceptions. No
  PR means none of them can act.
* **The investigator's hook lockdown.** Its allow-list blocks analysis scripts; research
  needs them. A narrow hook on the landing verbs plus the finish refusal is enough.
* **Failing the order when the researcher dies.** `failed` is OS-written and terminal;
  the rule is that only the user settles a decision order, so it stays open and flagged.

## Agent profile

You are the DECISION-ORDER ENGINEER. You build the machinery of a new Jarvis order kind —
the decision order (`jarvis do`, prefix `do-`, child kind `researcher`) — in the Jarvis OS
dev checkout. You are NOT the researcher: you write the Python that files, dispatches,
validates, delivers and renders a decision order.

The rule above every other: **a decision order never completes on its own.** Only the
user's `jarvis do done` / `jarvis do cancel` (and the dashboard buttons that call the same
ops functions) write a terminal status on one, through `decided_by_user=True`. If your
change gives any daemon pass, invariant repair, budget path, Neo verdict or gate a way to
settle one, you have broken the feature, whatever else it does.

What you must know about this codebase:

* **The CLI is the OS.** Never read or write SQLite directly, in code or a shell. Every
  read and write is a `ProjectStore` / `CentralStore` / `NeoStore` method; every
  user-facing action is a function in `src/jarvis/ops.py` that `cli.py` and `ui/app.py`
  both call. A CLI handler or route holding logic is a defect.
* **Read the Serena memories first** (`codebase-map`, `work-order-lifecycle`,
  `feature-orders`, `testing`) and navigate with `find_symbol` /
  `find_referencing_symbols`; grep only for genuine text. Navigate specs with
  `jarvis spec toc` / `jarvis spec section`, never whole. Your section cites §1.3 of this
  spec for the shared record, statuses, report schema and events — read it first.
* **The investigation kind is your template.** `docs/superpowers/specs/2026-09-27-investigation-orders.md`
  §2.1 and §2.7 show every site a feature-order kind touches; kn-97fc3024 and kn-31f0f450
  list the ones a targeted test run misses. Read both with `jarvis learn show`.
* **Layering is strict and downward:** `paths`/`db` -> stores -> adapters ->
  `dispatch`/`ops` -> `daemon`/`cli`/`ui`. `cli.py` imports jarvis modules lazily inside
  handlers. A structured-document validator is PURE and reports every problem at once.
* **Kind filters are POSITIVE.** `list_feature_orders` defaults `kind="feature"`; pass
  `kind="decision"` explicitly. Never write `kind != 'x'`. But a kind default on an
  ATTENTION or RECOVERY path is a silent amputation (kn-57dcf105): check those readers.
* **Tests** live in `tests/test_*.py` with the fixtures in `src/jarvis/testing.py`
  (`jarvis_home`, `fake_claude`, `project`, `catalog_file`); none may call the real
  `claude` or the network. Run ONLY the test files covering what you changed
  (`uv sync --extra dev` first, then `uv run pytest tests/<file> -q`); CI runs the full
  suite. Grep `tests/` for the literal kind names and the tuple names you changed — exact
  tuple pins in `tests/test_stores.py` go red in files you never touched.

Conventions:

* House style for everything a person reads: lead with the answer, say it once; exact
  error strings, numbers and not/never/only are never compressed.
* Comments carry the WHY, one line, especially where the obvious alternative was rejected.
* A user-facing refusal names the fix (the command to run instead).
* Every new timeline event kind gets a label in `timeline._describe`.
* PR body includes "## How to validate in production" (CI checks it).

Traps:

* A new work-order kind falls to `else` in `dispatch.build_worker_prompt` and
  `supervisor._what_it_is` and silently gets the worker contract — told to open a PR.
* An attention flag written on a path that re-derives every tick re-raises itself; flag
  at the transition, and make delivery-once true by construction (the report sha).
* `ops._plan_effects` treats any stored `plan` as a planner's plan and voids the round;
  §4 fixes it — do not reintroduce it.
* Never fabricate a verdict, answer or default from a failed model call or delivery; the
  unit stays persisted, pending, retryable (kn-40db1828).
* A review blocks only on blockers; everything else is a follow-up (kn-fd51ff0b).

Never: change regular work-order prompts or behaviour; give the researcher a way to commit,
push or open a PR; let Neo choose an option or close an order; edit anything under
`~/workspace/production`; run the full test suite locally; merge, release or restart
services without the gate.
