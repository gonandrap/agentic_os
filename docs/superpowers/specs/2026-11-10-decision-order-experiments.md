# Decision orders: an experiment phase whose results return to the same do

Feature order `fo-dc681c11`, filed from improvement order `io-d173b6f1`, finding
`experiment-left-decision-order`. It extends
`docs/superpowers/specs/2026-10-31-decision-orders.md` (called "the decision-orders spec"
below; navigate it by section with `jarvis spec section`).

## 1. Why, what is fixed, and the flow

### 1.1 The case

painforwisdom `do-a53854d1` recommended an A/B run. It was filed with `jarvis do follow-up`
as the ordinary work order `wo-da0e9bf6`. The decision order closed 44 s later, before any
data existed. The work order ran on the self-completing work-order lifecycle. Its results
ended up in one INFO notification and in `/tmp/ab-wo-da0e9bf6/aggregate.md`. The 5-hour
Max-subscription limit voided 2 of the pairs, and nothing had forecast that. The user never
got to read the results or ask about them.

The root cause: decision-orders spec §6 files every proposal as an ordinary wo, fo or
backlog item. An experiment's deliverable is data for the user's decision, yet it leaves the
only order kind built to wait for the user.

### 1.2 Fixed by the feature order (do not re-litigate)

1. An experiment follow-up runs UNDER THE SAME decision order. It is not a new order kind:
   `FO_KINDS` and `WO_KINDS` are unchanged, and the experiment runs as a
   `kind='researcher'` work order.
2. Its results land as a REVISION of the do's report (`feature_orders.plan`). The
   decision-orders spec §5.1 delivery path then delivers it as `revised`.
3. The do returns to `awaiting_decision` once the results are delivered, EVEN IF it was
   already `completed` when the experiment started.
4. Raw outputs are stored DURABLY on the project, not only in `/tmp` or the worktree.
5. A quota forecast is recorded BEFORE the batch runs (painforwisdom knowledge entry
   `kn-53702cc0`). The batch then waits for the user's go-ahead (§4.3). (Per Neo Q4169:
   see the closing note of §5.)
6. Must not: add an order kind, touch Claude artifacts (the Artifact tool or claude.ai
   pages), or change the regular work-order lifecycle.

### 1.3 The flow, end to end

```
do awaiting_decision | completed
  └─ user: jarvis do experiment <do> <key> | -d "brief"            (§4.1)
       do -> planning, flag down, experiments[n] appended, researcher revived or fresh
  └─ researcher: jarvis do forecast <do> --from-file forecast.json  (§5)
       forecast on the record; researcher settled; do flagged "forecast ready"
  └─ user: jarvis do experiment <do> --go                           (§4.3)
       go_at stamped, flag down, researcher revived with "go"
  └─ researcher runs the batch, then jarvis do attach <do> <file>…  (§6)
  └─ researcher: jarvis do report <do> --from-file report.json      (§4.4 gate)
       validation round (decision-orders spec §4)
  └─ daemon _deliver_decision: new sha -> awaiting_decision, flag, "revised" notification
  └─ user reads (jarvis do show), asks (jarvis do send), decides (jarvis do done)
```

The user can leave at any step. `jarvis do done` is allowed from `planning` once a report is
stored, which it always is here. `jarvis do cancel` is always allowed. Leaving abandons the
open experiment. No step lets the OS write a terminal status (decision-orders spec §2,
guard).

## 2. Shared vocabulary: events, metadata, the directory helper

This section lands first. Every other section imports what it defines.

**Events** (constants in `src/jarvis/project_store.py`, beside `DECISION_DELIVERED_EVENT`).
They are written on the timeline of the experiment's researcher work order:

| constant | name | data |
|---|---|---|
| `DECISION_EXPERIMENT_STARTED_EVENT` | `decision_experiment_started` | {do_id, n, key, reopened_from} |
| `DECISION_FORECAST_EVENT` | `decision_forecast_submitted` | {do_id, n, runs, total_tokens, window_share_pct, meter} |
| `DECISION_EXPERIMENT_GO_EVENT` | `decision_experiment_go` | {do_id, n} |
| `DECISION_ATTACHED_EVENT` | `decision_attached` | {do_id, n, name, size, sha256} |

**Experiment state lives in `feature_orders.metadata["experiments"]`, NEVER in `plan`.**
`submit_decision_report` replaces `plan` whole, so state kept inside the report would be
wiped by the results it is waiting for. The shape is a list, in order, one entry per
experiment:

```json
{"n": 1, "key": "p1", "title": "...", "brief": "...", "researcher_id": "wo-…",
 "started_at": 0.0, "reopened_from": "completed|awaiting_decision",
 "forecast": null, "go_at": null, "attachments": [], "reported_at": null}
```

* `key` is the proposal key, or `""` for an ad-hoc experiment (`-d`).
* `forecast` is §5's stored object.
* Each `attachments[]` entry is `{name, size, sha256, path, at}`. `path` is relative to the
  project root.
* `reported_at` is stamped by §4.4 when a results report is accepted.

**Store helpers** on `ProjectStore`. They are pure reads and writes of that metadata, with
no policy. They use the same read-modify-write the other metadata keys use (see
`superseded_children`), inside `db.write_transaction`:

* `experiments_of(do_id) -> list[dict]`
* `open_experiment(do_id) -> dict | None`: the last entry with `reported_at` null, else
  `None`.
* `append_experiment(do_id, entry) -> int`: returns `n`.
* `update_experiment(do_id, n, **fields)`
* `add_experiment_attachment(do_id, n, mark)`

**Phase** is a pure function in `decision_report.py`:
`experiment_phase(entry) -> "forecast_due" | "awaiting_go" | "running" | "reported"`. It is
derived from `forecast` / `go_at` / `reported_at`. §4, §5 and §7 all read it, so it is
defined once.

**The proposal type.** `decision_report.ORDER_TYPES` gains `"experiment"`, so a report may
propose `{"as": "experiment"}`. Until §4 lands, `file_decision_followups` already refuses an
`as` outside `_FOLLOWUP_AS` with a clear message, so this widening is safe on its own.

**The directory helper**, in `src/jarvis/specs.py` beside `feature_dir`:
`experiment_dir(project_path, do_id, n, key) -> Path`. It returns
`feature_dir(project_path, do_id) / "experiments" / f"{n}-{key or 'adhoc'}"`. It is not
created here. `set_feature_status` removing `feature_dir/agent/` on a terminal status must
NOT remove `experiments/`; pin that in a test.

**Tests:** round-trip of each helper; `open_experiment` across two entries;
`experiment_phase` for all four phases; `parse_report` accepts `as: "experiment"`; a
terminal status leaves `experiments/` on disk.

## 3. The researcher's contract: proposing an experiment

This section owns what the researcher is TOLD about experiments when it first researches.
It does not own the brief sent when an experiment starts; §4 owns that.

* `dispatch._researcher_prompt` gains one rule. A follow-up that must RUN something to
  produce data for this decision is proposed as `"as": "experiment"`, never `"wo"`. Examples
  are an A/B, a replay, a benchmark, or a batch over fixtures. Its `description` states what
  to run, on which inputs, how many runs, and what numbers come back. An experiment is a
  phase of this same decision order: it reports back here, and the user starts it. A `wo` is
  for work that changes code or produces a document.
* `decision_report.SCHEMA_TEXT` lists `experiment` among the `as` values, with that
  one-line meaning. The brief embeds `SCHEMA_TEXT`, so the two cannot drift.
* `neo.RESEARCHER_RULE` already sends "which option to take" questions to the user. It
  gains one clause: "whether to run an experiment, or how big to make it, is the user's
  call too".

**Tests:** the researcher prompt names `as: "experiment"`; `SCHEMA_TEXT` lists it; a regular
worker prompt is byte-identical before and after (decision-orders spec §1.2 item 1).

## 4. Reopen and run: `jarvis do experiment`, `--go`, and the results gate

This section owns the experiment state machine on the do. It covers starting, the go-ahead,
accepting results, and family-wide delivery. These are not split: each half alone would
leave `planning` decision orders with no live path forward.

### 4.1 Start: `ops.start_decision_experiment(do_id, key=None, brief="", project_name=None)`

* REFUSED when `JARVIS_WO_ID` is set. This is the user's verb: it spends quota and reopens
  the order. The message names the verb for the user.
* Accepted only from `awaiting_decision` or `completed`. The refusal messages are:
  `planning` gives "an experiment or the research is already running — jarvis do show";
  `cancelled` gives "file a new do"; `budget_exhausted` names `jarvis do budget <do> <usd>`,
  because a budget-held researcher would silently queue the message.
* Refused when `open_experiment(do_id)` exists (one experiment at a time).
* Exactly one of `key` and `brief` is given. A `key` must name a proposal of the stored
  report. Any `as` value is accepted, because `do follow-up --as experiment` routes here.
  The title is the proposal's title, or the first line of `brief`.
* In ONE transaction, in this order:
  1. `set_feature_status(do, "planning", trigger=DECISION_EXPERIMENT_STARTED_EVENT)`. This
     is a non-terminal write, so the guard allows it.
  2. `clear_feature_attention(do)`. The user is no longer owing the decision; they are
     waiting for data.
  3. `append_experiment(...)`.
  4. Then the researcher (below), and the started event on its timeline.

  Status goes first and the child after it. A settled do with a live child trips the
  orphan-child invariant.
* **Researcher: revive or fresh.**
  * Revive the existing `plan_wo_id` ONLY when that work order is `completed` AND its
    worktree still exists on disk. `disk.sweep` reclaims the worktree of a settled order
    after `DEFAULT_WORKTREE_RECLAIM_AFTER_HOURS`. To revive, call
    `ops.send_message(researcher, brief_text, source="jarvis")`.
  * Otherwise, open a FRESH `kind="researcher"` child the way `resume_feature_order` does.
    That means `store.create_work_order` directly, `origin="os"` and `parent_id=do`.
    `ops.create_work_order` refuses a settled parent. The description is the do's
    description plus the brief. Then repoint `plan_wo_id`.
  * Record the chosen `researcher_id` on the entry.
  * A failure after step 1 leaves a `planning` do with no live researcher. The existing
    §5.3 pass of the decision-orders spec flags that on the next tick. That is the intended
    self-heal; do not add an invariant.
* **The brief** is `decision_report.experiment_brief(fo, entry, proposal) -> str`, pure,
  owned here. It says, in order:
  1. You are running experiment n of decision order <do>, for the user's decision.
  2. The experiment: the proposal's description verbatim, or the ad-hoc brief.
  3. FIRST, forecast it and submit `jarvis do forecast <do> --from-file forecast.json`
     (embed §5's schema text). That ends your turn. Do NOT start the batch; the user says
     go.
  4. On go: run it. Keep raw outputs in your temp dir, then
     `jarvis do attach <do> <file>…` every raw output the findings rest on.
  5. Resubmit the WHOLE report with `jarvis do report`. The results go in `findings` (each
     with evidence naming the attached files) and an updated `summary` / `recommendation`.
     Keep the earlier findings that still hold.
  6. Never file orders, never decide.
* Return `{do_id, n, researcher_id, revived: bool, status: "planning"}`.

### 4.2 `jarvis do follow-up <do> <key> --as experiment`

`file_decision_followups` gains `experiment`. A key that resolves to `experiment` (its own
`as`, or `--as`) is NOT filed as an order. It calls `start_decision_experiment(do, key)`
and is reported in the result under `started`. With `--all`, at most one experiment may
start. A second experiment key is reported under `errors` ("one experiment at a time").
Filed `wo`/`fo`/`backlog` keys behave exactly as today. Do not stamp `filed` for an
experiment: `experiments[]` is its record.

### 4.3 Go-ahead: `ops.approve_decision_experiment(do_id, project_name=None)`, CLI `jarvis do experiment <do> --go`

* Refused when `JARVIS_WO_ID` is set.
* Requires the open experiment's phase to be `awaiting_go`.
* Stamps `go_at`, clears the do's attention flag, and writes `DECISION_EXPERIMENT_GO_EVENT`.
* Sends the researcher a message: "go: run experiment n as forecast; attach raw outputs;
  then resubmit the report".
* The user may instead `jarvis do send` to change the plan. The researcher then
  re-forecasts: a second `do forecast` replaces the stored one while the phase is
  `awaiting_go`.

### 4.4 The results gate, in `ops.submit_decision_report`

While `open_experiment(do)` exists, a report is REFUSED unless all of these hold:

* the phase is `running` (forecast recorded AND `go_at` set);
* at least one attachment is recorded;
* `decision_report_sha(new plan)` differs from the sha of the last `decision_delivered`
  event. Identical results would never deliver, and the do would sit in `planning`.

Each refusal names the missing step's command. On acceptance, stamp `reported_at`. Nothing
else in submission changes: validation and delivery stay exactly as the decision-orders
spec §3–§5 say.

### 4.5 Family-wide delivery

`ProjectStore.last_decision_delivery` is keyed per researcher work order today. A fresh
experiment researcher would therefore deliver its results titled "ready", not "revised".
Add `last_decision_delivery_of(do_id)`, which scans the `decision_delivered` events of every
`kind='researcher'` work order whose `parent_id` is the do, newest first. Switch
`Daemon._deliver_decision` to it. The delivery-once sha comparison is unchanged.

**Tests (in `tests/test_decision_orders.py` or a sibling
`tests/test_decision_experiments.py`):**

* Start from `completed` gives `planning` with the flag down. Then a forecast, go, attach
  and report. A `daemon.tick()` then delivers it as `revised`, giving `awaiting_decision`
  with the flag up and exactly one new notification.
* The same run starting from `awaiting_decision`.
* Revive when the worktree exists; fresh child when it is reclaimed (fake the missing
  path) or the researcher is cancelled. `plan_wo_id` is repointed; "revised" is still
  detected.
* Each refusal: env set, a wrong status, budget_exhausted, a second experiment, report
  before go, report with no attachment, report with an unchanged sha.
* `do follow-up --as experiment` starts rather than files.
* `do done` from `planning` mid-experiment closes the order. Repeated ticks never write a
  terminal status; extend the never-auto-complete suite with an experiment phase.

## 5. The quota forecast: `jarvis do forecast`

This section owns the forecast's schema, its arithmetic, recording it, and telling the user.

**Researcher input** (`forecast.json`). The researcher states what it knows; the OS computes
what it can measure:

```json
{"runs": 24, "model": "claude-…", "per_run": {"input_tokens": 40000, "output_tokens": 3000,
 "minutes": 2.5}, "notes": "4 fixtures x 2 engines x 3 repeats"}
```

Parse it in a pure module, `src/jarvis/experiment_forecast.py`. It has no database and no
LLM. `parse_forecast(doc)` raises `ForecastError(problems)` listing every problem: `runs`
≥ 1; token counts ≥ 0; `model` non-empty; caps of `runs` ≤ 10000 and `notes` ≤ 1000 chars.
It also exposes `SCHEMA_TEXT` (embedded by §4's brief) and
`compute(forecast, meter) -> dict`, which adds:

* `total_tokens` and `minutes_total`;
* `est_usd` at the per-model token pricing `jarvis cost` already uses (find it; do not add
  a price table);
* `points_per_run` = run usd / `usage_meter.dollars_per_point()`;
* `window_share_pct` = total points;
* `window_used_pct` = `usage_meter.latest_sample()`'s `five_hour_pct`, and its `resets_at`;
* `max_safe_runs_now` = floor((100 − used − 10) / points_per_run), with 10 points of
  headroom;
* `fits_now: bool`.

When there is no meter sample or no OAuth token (an API-key fleet), set
`meter: "unavailable"` and leave the measured fields null. The forecast is still recorded
and still gates (§4.3). `compute` takes the meter values as arguments, so tests need no
network.

**`ops.submit_decision_forecast(do_id, doc, project_name=None)`:**

* Requires the open experiment's phase to be `forecast_due` or `awaiting_go`, which allows
  a re-forecast.
* Stores `forecast` (input plus computed) with `update_experiment` and writes
  `DECISION_FORECAST_EVENT`.
* SETTLES THE RESEARCHER IN-SESSION WITHOUT A VALIDATION ROUND. A forecast is not a
  deliverable. Use the existing settle-without-panel path that in-session kinds use; do not
  change the researcher's `SETTLE_BY_FINISH` contract. A researcher that just ended its
  turn would settle `needs_review` ("idle without finish") with the wrong flag.
* Then `store.raise_decision(do, "planning", reason, title=…, body=…)`. The reason is
  `"experiment n forecast: <runs> runs, ~<window_share_pct>% of the 5h window (now
  <used>% used) — jarvis do experiment <do> --go"`. The body is the rendered forecast.
  `raise_decision` already flags and notifies at `warning` through the user's sinks.

The CLI `jarvis do forecast <do> --from-file <path|->` is a thin wrapper. The researcher's
Bash hook must allow it. Check `researcher_bash_decision` and the decision-close regex;
neither should need a change, so pin it with a test.

**Tests:** parse errors are listed together; `compute` with a meter sample and with
`unavailable`; submit settles the researcher with no round opened and flags the do once;
a re-forecast while `awaiting_go` replaces the stored forecast; submit is refused in the
`running` phase.

Gate policy, per Neo Q4169: the go-ahead is required on every experiment (§4.3). The OS has
no billing-mode setting, so "only on the Max subscription" cannot be keyed reliably. When
the meter is unavailable the forecast is marked as such, not skipped.

## 6. Durable raw outputs: `jarvis do attach`

This section owns copying raw outputs onto the project, and the manifest.

**`ops.attach_decision_outputs(do_id, paths, project_name=None)`, CLI
`jarvis do attach <do> <path>...`:**

* Requires an open experiment in phase `awaiting_go` or `running`.
* Each path must be an existing regular file. Directories are refused with "pass the files,
  or tar them first". Symlinks are resolved and then checked as files.
* Caps (module constants): ≤ 50 MB per file, ≤ 500 MB per experiment, ≤ 200 files per
  experiment. A refusal names the cap and files nothing from that call: check everything
  first, then copy.
* Copy to `specs.experiment_dir(project_path, do, n, key) / <basename>`, creating the
  directory. A name clash gets a `-2`, `-3` suffix; never overwrite.
* For each file, record `{name, size, sha256, path, at}` with `add_experiment_attachment`
  and write `DECISION_ATTACHED_EVENT`.
* Return the stored paths, absolute, so the researcher can cite them as evidence.

Copying is done by the CLI process, not by the agent's Write tool. So the researcher write
hook (`hooks.researcher_write_decision`) needs NO change. That hook only allows
`report.json`, the in-worktree scratch dir and the temp dir, which is why outputs landed in
`/tmp`. The directory lives under the project's `.jarvis/features/<do>/`. It is not in the
worktree, so `disk.sweep` never reclaims it. Do not add it to any cleanup.

**Tests:** copy plus manifest plus sha256; every cap refusal files nothing; a clash gets a
suffix; refused with no open experiment; the files survive `do done`.

## 7. Surfaces: show, dashboard, timeline, operator docs

This section owns every read surface. It changes no state.

* `ops.show_decision_order` adds `experiments`: each entry plus its `experiment_phase`. The
  CLI `jarvis do show` prints, after the report, one block per experiment: n, title,
  phase, the forecast line (runs, share, `fits_now`, or "meter unavailable"), and the
  attachments with absolute paths and sizes. When the open experiment's phase is
  `awaiting_go`, it prints the `--go` command. `--json` carries it all.
* The `ui/app.py` decision page shows the same block. It adds a "Go" button posting to a
  new route that calls `approve_decision_experiment`, beside the existing `decision_send`,
  `decision_done` and `decision_cancel` routes, with the same CSRF and redirect pattern.
* `timeline._describe` gets a label for each of §2's four events (`kn-3f133363`).
* Operator docs: the decision-order page under `docs/os/operator/` gains an "Experiments"
  subsection covering the flow in §1.3 and the five commands. In the repo `CLAUDE.md`
  command crib sheet, ONE line after the `jarvis do` line:
  `jarvis do experiment <id> <key>|-d "…" [--go]   # run a proposal UNDER the do; results come back as a revision`.
  `evals/llm/test_jarvis_judgment.py` loads `CLAUDE.md`; run that eval file's offline
  tests.
* The rule that costs the user if forgotten, added to the `CLAUDE.md` rules list in one
  sentence: a decision order's experiment follow-up goes to `jarvis do experiment`, never
  `jarvis wo create`.

**Tests:** `do show` text and json with no experiment, a forecast awaiting go, and a
reported experiment with attachments; the ui page renders each phase; the Go route calls
the op; the timeline labels.

## 8. Out of scope

* A new order kind, or a new feature-order status. `planning` ("researching") covers a
  running experiment, so the render vocabulary is unchanged.
* Claude artifacts, and artifact comments as an inbound channel (finding
  `artifact-comments-not-durable` of `io-d173b6f1`, still with Neo).
* Changing `jarvis wo finish --abandon` (finding `abandon-voids-results`, `wo-cff6fcc4`).
* Surfacing a reply hint on ordinary completed work orders (finding
  `conversation-path-hidden`).
* Several experiments in parallel under one do.
* Rescuing `wo-da0e9bf6`'s `/tmp` results. That is a one-off; the user can `jarvis wo send`
  it.
* A billing-mode config key.

## 9. Rejected alternatives

* **An `experiment_order` kind with a warm artifact.** It duplicates the do lifecycle,
  which already never auto-completes and already revives the researcher on `do send`. And
  an artifact is not a durable inbound channel.
* **An experiment as a child `wo` under the do.** A `kind='worker'` child runs the
  self-completing lifecycle and opens a PR, which is the original failure. It would also
  make `settle_features` / `feature_children(WORKER_CHILDREN)` see a decision order.
* **Always reviving the old researcher.** Its worktree is reclaimed 24 h after it settles,
  and `do done` cancels a live one. Revival is used only when it is safe (§4.1).
* **Keeping experiment state in the report.** Every resubmission replaces `plan` whole
  (§2).
* **Forecast as record-only.** It would not have stopped the 5-hour-limit loss in
  `do-a53854d1`, and the user's rule asks for a go-ahead.

## Agent profile

You are a Jarvis OS developer working on ONE section of the decision-order experiment
phase. The spec is `docs/superpowers/specs/2026-11-10-decision-order-experiments.md`. It
extends `docs/superpowers/specs/2026-10-31-decision-orders.md` (the "decision-orders
spec"). You build in the dev checkout's worktree you were started in. You end with a pull
request against `main`, whose body follows the repo template, including
"## How to validate in production".

What you must know:

* A decision order (`do-…`) is a `feature_orders` row with `kind='decision'`. It has a
  `kind='researcher'` work order linked by `plan_wo_id` and `parent_id`. Its report is the
  JSON in `feature_orders.plan`, replaced WHOLE on every submission. Its statuses are
  `pending`, `planning` ("researching"), `awaiting_decision`, `budget_exhausted`,
  `completed` and `cancelled`. Only the user's `jarvis do done|cancel` writes a terminal
  status. `ProjectStore.update_feature_order` raises `DecisionOrderGuard` otherwise, and
  only `ops._user_closes_decision` passes `decided_by_user=True`. Never add another site
  that passes it.
* Delivery is `Daemon._deliver_decision`. When the researcher has `completed` and the report
  sha (`project_store.decision_report_sha`) differs from the last delivered sha, it moves
  the order to `awaiting_decision`, raises the flag, and notifies through
  `store.raise_decision`. Delivery-once comes from the sha comparison, never from a flag
  read.
* Experiment state lives in `feature_orders.metadata["experiments"]` (§2 of the spec),
  never in `plan`.
* User verbs (`do experiment`, `--go`, `done`, `cancel`) REFUSE when `JARVIS_WO_ID` is in
  the environment. Researcher verbs (`do forecast`, `do attach`, `do report`) are run by the
  researcher.
* The CLI is a thin wrapper over `ops`. Business logic goes in `ops.py` or a pure module;
  the store holds no policy.

Conventions:

* Navigate Python with Serena symbol tools (`find_symbol`, `find_referencing_symbols`,
  `get_symbols_overview`). The hooks refuse `sed`/`cat` on Python. Navigate specs with
  `jarvis spec toc|section`; never read one whole.
* Every refusal message names the exact command that fixes the situation. A test asserts
  that the named command is accepted.
* Put new events' constants in `project_store.py`, and give each a `timeline._describe`
  label.
* Run only the targeted tests: `uv run pytest tests/<file> -q`. Never run the full suite
  locally; CI does.
* Comments say only the why the code cannot show, in one line. House style for PR text and
  commit messages.

Traps:

* A new work-order kind falls to the generic worker contract and is told to open a PR. Do
  not add one; the experiment runs as `kind='researcher'`.
* `ops.create_work_order` refuses a settled parent. To open a fresh researcher under a
  reopened do, use `store.create_work_order` as `resume_feature_order` does.
* Reviving a researcher whose worktree `disk.sweep` reclaimed gives a session with no
  checkout. Check that the worktree exists before reviving.
* A researcher that ends its turn without settling becomes `needs_review` ("idle without
  finish"). Every researcher verb that ends a turn (`do forecast`, `do report`) must settle
  it.
* Set status BEFORE you create a child under a reopened do. A settled do with a live child
  trips the orphan-child invariant.
* `ops.send_message` clears the attention flag of the WORK ORDER, not of the do. Clear the
  do's flag explicitly with `clear_feature_attention`.
* Regular work-order prompts, the default validation roster and the work-order lifecycle
  must be byte-for-byte unchanged. Pin that where you touch a shared path.

Never:

* Write a terminal status on a decision order, or pass `decided_by_user=True`.
* Touch Claude artifacts, add an order kind, or add a feature-order status.
* Edit outside your section's files, except the tests and fixtures your change breaks.
  Another child owns the other sections; leave their functions alone.
* Write anywhere under `~/workspace/production`.
