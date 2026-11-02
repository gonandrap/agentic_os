# Monitoring orders — Morpheus without a long-lived session

Feature order `fo-4bda731e`. Supersedes the "Morpheus's session writes the checks" half of
`docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md`; the rest of
that spec (queueing, mechanical checks, hand-off routing) stays true unless a section below
changes it.

## 1. Why, what is fixed, and the shared interfaces

### 1.1 The problem

Morpheus is ONE resumed Claude session (`morpheus.wake`, `claude_cli.run_session_turn`,
state in os_state `morpheus.session`). It idles past the prompt-cache TTL between releases,
so each release intake cold-writes its whole context (observed 2026-11-02 01:22 UTC:
321,883 tokens, then a 367k mid-burst re-write, then a compaction), and its prompt
re-offers every still-queued PR (18 in that intake). A release should cost what its NEW PRs
need, nothing more.

### 1.2 Fixed by the user (do not re-litigate)

* A MONITORING ORDER (MO) is an order kind: id prefix `mo`, its own CLI
  (`jarvis mo …`), dashboard view, search and `jarvis cost` attribution.
* Input: a prompt (an agent writes the check from it) OR a ready script, plus a frequency
  and a deadline.
* Creating one opens a SHORT session that writes (prompt) or validates (script) the check,
  persists it, and ends. No session survives between orders or between turns of one order.
* The daemon runs checks on the frequency, mechanically, at no model cost.
* Success settles quietly. Anything else (failed, timed out) opens a NEW short session,
  built from the persisted row (never from conversation history), that judges it:
  rewrite the script, confirm a real failure (and get the fix filed), or escalate.
* Releases add MOs only for the PRs they newly carry. Armed orders are never re-armed.
* Today's `mv-*` records become MOs. Morpheus's verbs stay as MO operations.
* Cost accounting is correct from day one: one usage per API message, priced at the model
  that actually ran (issue #1462).

### 1.3 Shared interfaces (every section relies on these; none owns them alone)

**Record.** One central table `monitoring_orders` in `os.db` (§2), cross-project, NOT a
`feature_orders` kind: FO kinds are per-project and run their child as a full worker in a
worktree, which is the briefing cost this feature removes. `project_store.is_feature_order_id`
must NOT learn `mo-` (about 40 ops sites branch on it).

**Ids.** Existing rows KEEP their `mv-` ids (they are stored by equality in neo.db question
context `morpheus_item`, `feature_orders.metadata[ops.MORPHEUS_ITEM_KEY]`, steward payloads,
`morpheus_runs.item_id` and inbox bodies). New rows mint `mo-xxxxxxxx`. ONE predicate,
`morpheus.is_monitoring_order_id(x) -> bool`, accepts both prefixes; every surface uses it.

**States — unchanged** (`morpheus.STATES`), because invariants and the UI key on them, plus
one new terminal state `cancelled`:

| state | meaning |
|---|---|
| `queued` | not armed yet. ARMING IS OWED while `queued` and `attempts < MAX_OFFERS` and `last_attempt_ts` is older than `REOFFER_SECONDS` |
| `monitoring` | armed; the daemon runs `script` every `every_minutes` |
| `validated` | exit 0, or waived — terminal, quiet |
| `failed` / `timedout` | terminal; JUDGING IS OWED while `action` is empty |
| `cancelled` | `jarvis mo cancel` — terminal, never judged, never handed off |

A `queued` order past its deadline times out exactly as today (`sweep`), which is how an
order nobody could arm surfaces by itself.

**New columns** (§2): `title`, `origin` (`release|merge|user|investigation|steward`),
`prompt` (what to monitor, for non-PR orders; PR orders keep using `hints`),
`every_minutes` (NULL = `os.morpheus.check_every_minutes`), `attempts`, `kills`,
`last_attempt_ts`, `last_session_id` (integers default 0, text default ''). `pr_number` becomes nullable. No `source` column:
"`script` empty" says a prompt order is unarmed, and `script_source` (`agent|user`) exists.

**The session runner** (§3 builds it, §4 reuses it):

```python
def run_session(central, catalog, item: dict, *, purpose: str,   # "arm" | "judge"
                cwd: Path, call=claude_cli.run_headless_result) -> dict | None
```

One fresh `claude -p` call, never resumed: `cwd` = the order's production directory (the
`dirs` map `Daemon._morpheus_pass` already builds), `tools="Bash,Read,Glob,Grep"`,
`permission_mode=catalog.os.default_permission_mode`, `model=cfg.model`,
`timeout=cfg.turn_timeout_seconds`, `records_itself="morpheus"`, `persist=True` (§3). Its
prompt is built ONLY from the row. It returns the parsed JSON reply, or None on a failure
(never a fabricated verdict — kn-40db1828). The session never runs `jarvis`: it returns
the script in its reply and the OS writes it, so the session can never write into the wrong
`JARVIS_HOME`.

**Accounting contract.** Every session records exactly one `agent_calls` row via
`agent_usage.record(agent_usage.MORPHEUS, usage=…, wo_id=item["id"],
project=item["project"], label=purpose, session_id=…)`. Its usage counts each API message
ONCE (deduplicated by message id) and prices each at the model that ran it. Work order
`wo-deef2884` (issue #1462, in flight outside this feature) fixes how Morpheus usage is
derived; §3 uses whatever helper that order lands and must not re-implement it. `wo_id`
carrying the MO id is what makes `jarvis cost <mo-id>` possible (§7).

**`morpheus.report()` shape after §3:** keys `enabled, counts, paused, pause, activity,
last_release, items`. The `session` key is gone. `activity` is
`{"state": "arming"|"judging"|"idle"|"paused"|"usage_held", "owed_arm": int,
"owed_judge": int, "usage_hold_until": float|None, "max_offers": int}`, derived from the
rows and the hold, never from a session. §5 and §6 render exactly this.

**One call, one row.** `run_session` makes exactly ONE `claude -p` call per invocation and
parses its reply with `structured.coerce` (no in-session retry: `structured.request`
cannot pass `tools`/`persist`, and a retry would be a second call and a second usage row).
It records usage itself from the `HeadlessResult`, which carries `session_id`. An
unparseable reply is a None: one attempt spent, no verdict.

**Owed-work queries** (§3 and §4 call them; §2 provides them on `CentralStore`, keeping the
existing `morpheus_*` method names): `morpheus_items(states)`, `update_morpheus_item`,
`claim_morpheus_action(id, action) -> bool` (atomic, empty action only).
`attempts` counts failed sessions for the order's CURRENT owed step: a successful arm
resets it to 0, so the judge starts with a full `MAX_OFFERS`.

## 2. The record: `monitoring_orders`, its migration and its id

Owns `src/jarvis/central_store.py` (schema lines ~561-602, `ADDED_COLUMNS` ~648, the
offboard map `live_rows_by_project` ~997, the `morpheus_*` methods ~2337-2426) and, in
`src/jarvis/morpheus.py`: `STATES`/`TERMINAL_STATES` (+ `cancelled`), `enqueue`,
`enqueue_release`, `queue_merges`, `set_script`, `check` (it schedules `next_check_ts`),
plus new `create`, `cancel` and `is_monitoring_order_id`. Also the exact table pin in
`tests/test_purge_project.py` (`SCOPE_TABLES`, `len(derived) == 11`).

* **Rebuild, not ALTER.** `morpheus_items` has `pr_number INTEGER NOT NULL` and
  `UNIQUE (repo, pr_number)`, which SQLite cannot drop. Migration, idempotent, in one
  transaction: create `monitoring_orders` (every existing column, `pr_number` nullable, the
  §1.3 new columns), copy every row with its id, drop `morpheus_items`, then
  `CREATE UNIQUE INDEX … ON monitoring_orders(repo, pr_number) WHERE pr_number IS NOT NULL`.
  `morpheus_runs` keeps its name and `item_id`. A home already migrated is a no-op; a fresh
  home creates `monitoring_orders` directly.
* **Offers move onto the row.** Today the offer count lives in
  `os_state['morpheus.session']['offers']`. The migration copies each item's `n`/`ts` into
  `attempts`/`last_attempt_ts`. Nothing else reads that key afterwards (§3 deletes it).
* **Method names stay** (`add_morpheus_item`, `morpheus_items`, `update_morpheus_item`,
  `claim_morpheus_action`, …). Only the SQL behind them changes, so no caller moves.
* **`create(central, *, project, title, prompt="", script="", every_minutes=None,
  deadline_days=None, origin="user") -> dict`** mints `mo-`, requires exactly one of
  `prompt`/`script`, state `queued`, `script_source="user"` when a script is given. Release
  and merge queueing set `origin` `release`/`merge` and mint `mo-` for new rows; the
  `(repo, pr_number)` uniqueness keeps a release from re-adding a PR already present in
  any state — that is the "only new PRs" rule, pinned by a test.
* **`cancel(central, mo_id)`**: `queued`/`monitoring` -> `cancelled`, `next_check_ts` NULL.
  Refuses a terminal row.
* **`check`** schedules `next_check_ts` from the row's `every_minutes` (fallback
  `cfg.check_every_minutes`). `sweep` never touches `cancelled`. `OPEN_STATES` is
  unchanged; `cancelled` joins `STATES` and `TERMINAL_STATES`, not `ACTION_STATES`.
* **Tests** (`tests/test_morpheus.py`, `tests/test_purge_project.py`): seed a db from the
  LITERAL old `morpheus_items` DDL (not today's SCHEMA) plus session offers; opening the
  store keeps ids/state and copies offers into `attempts`/`last_attempt_ts`; a second open
  changes nothing and `morpheus_items` does NOT exist afterwards (SCHEMA must no longer
  create it); two PR-less `create` rows coexist; `check` honours `every_minutes`;
  `is_monitoring_order_id` true for `mv-`/`mo-`, false for `fo-`/`wo-`. The existing
  `test_a_release_queues_only_what_it_added` stays green.

Does NOT touch `wake`, `act`, the CLI, the UI or cost.

## 3. Arming in a short session; the long-lived session goes away

Owns, in `src/jarvis/morpheus.py`: the new `run_session` and `arm`; the deletion of `wake`,
`session_state`, `_save_session`, `wanting_scripts` (replaced by the owed-arming query on
the row), `build_prompt` (replaced by a per-order prompt), `context_share`, `kill_hold`,
`_mark_offered`, `_offer_cause`, `_forget_offer`, `SESSION_KEY`; and the reshaping of
`activity`, `report` and `MORPHEUS_PERSONA`. Also `claude_cli.run_session_turn` (its only
caller is `wake`: delete it), a `persist: bool = False` parameter on
`claude_cli.run_headless_result` (when true, omit `--no-session-persistence` so the
transcript exists for accounting and `jarvis inspect`), the `wake` call in
`Daemon._morpheus_pass` (`daemon.py` ~3366), `morpheus.rewrite`'s re-arm,
`scripts/screenshot_morpheus.py` (uses `_save_session`, `MAX_KILLS`), and the MINIMAL
render edits that keep `jarvis morpheus status` and `/morpheus` working on the new
`report()` shape: `cli.cmd_morpheus`'s `rep["session"]` reads, `cli._morpheus_activity_text`
(reads `turn_in_flight`, `turn_kind`, `held`, `kill_hold_until`), and the session block of
`ui/templates/morpheus.html`. §5 and §6 own everything else in those files.

* **`arm(central, catalog, cwd_for, *, now, call, skip) -> dict[str, int]`**: for each
  order owed arming (§1.3), oldest first, ONE `run_session(purpose="arm")` at a time, on
  the existing `morpheus_pool` thread — sequential so the identical system prompt hits the
  5-minute cache across a release's orders. The prompt carries the row only: title, PR
  link and `hints` (PR orders) or `prompt` (user orders), `rewrite_brief` if set, the
  existing script if `script_source="user"`, and the exit-code contract (0 validated, 1
  only on the change's own failure signature, 75 cannot read the environment).
* **Reply** `{"script": "<bash>", "notes": "…"}` or `{"refuse": "<why>"}`, parsed per
  §1.3 "One call, one row". A script reply: `set_script(…, source=<agent|user
  as before>)`, state `monitoring`, `last_session_id` stored, `attempts = 0`. A user script the session
  returns unchanged keeps `script_source="user"`. A refusal or None: `attempts += 1`,
  `last_attempt_ts = now`, state stays `queued`.
* **Bounds, all per row** (kn-1e43fe1a): `KilledError` and `UsageLimitError` cost no
  attempt. A usage-window refusal sets ONE global hold in os_state `morpheus.usage_hold`
  (`arm`/`judge` return at once until it passes — the window is the account's, not the
  order's). Kills count in the row's `kills` column (§2); an answered session resets it;
  three consecutive kills spend one attempt, so a deterministic kill still ends at
  `MAX_OFFERS`.
* **Removed with the session:** compaction (`compact_at` stays PARSED by
  `catalog.MorpheusConfig` so existing catalogs load, and is documented as ignored), the
  kill-hold and its inbox warning, the `morpheus.session` os_state key (deleted on first
  pass).
* **Pause:** `arm` respects the fleet pause and the Morpheus pin exactly as `wake` did
  (`morpheus.pause_state`); checks never pause.
* **`morpheus.rewrite`** (also reached from `ops._script_wrong_rewrite`) puts the row back
  in `queued` with `rewrite_brief`, `attempts = 0`; `arm` picks it up. No session state.
  It keeps `script_source="user"` and the user's script when the row had one, so the arm
  session revalidates it instead of silently replacing it.
* **`report()`/`activity()`**: per §1.3; `activity` reports `arming`/`judging`/`idle`/
  `paused`/`usage_held` from the rows and the hold, never from a session.
* **Tests:** a release adding 3 orders beside 15 `monitoring` rows makes exactly 3 calls
  (fake `call`) with `persist=True`, and the 15 rows' `script` is unchanged; each
  records one `agent_calls` row whose `wo_id` is its order id and whose label is `arm`; a
  None reply spends one attempt and no state change; `MAX_OFFERS` stops further calls;
  usage hold blocks all; `run_session_turn` and `SESSION_KEY` no longer exist (grep pin);
  `run_headless_result(persist=True)` omits `--no-session-persistence` — proven by
  patching `claude_cli._run` and reading argv, not by faking `run_headless_result`;
  `jarvis morpheus status` and GET `/morpheus` still succeed.

Does NOT touch `act`, Neo's `validation` path, the CLI verbs, or templates beyond removing
what renders `report()['session']` (`ui/templates/morpheus.html` session block) so the UI
does not break.

## 4. Judging a failure in a short session

Owns `morpheus.act` and its helpers (`owed_actions`, `_action_kind`, `_investigate`,
`_improve`, `_why`, `_ask_neo`, `verdict_action`, `stale_question`,
`record_neo_verdict`), the `act` loop in `Daemon._morpheus_pass` (~3356), the daemon's
`validation` question branch (~3522), the validation question text (`ops.py` ~10534),
steward candidates with origin `morpheus` (`steward.py` ~444-468), and the invariants
`check_morpheus_moves` (~3865) and the stale-validation-question check (~2133).

* **Owed:** a `failed`/`timedout` row with empty `action`, not `cancelled`. Claimed
  atomically with `claim_morpheus_action(id, "judge:<ts>")` before the session runs, so two
  passes never judge one order.
* **Prompt, from the row only:** title, PR/hints or prompt, the script, the last
  `morpheus_runs` (exit + output tail, newest 5), the deadline and why it timed out,
  `script_rewrites` and `auto_requeues` so far.
* **Reply** `{"verdict": "rewrite"|"confirm"|"escalate", "reason": "…", "script": "…"}`
  (`script` only with `rewrite`), parsed per §1.3 "One call, one row".
  * `rewrite` and `script_rewrites < MAX_SCRIPT_REWRITES`: set the new script, state
    `monitoring`, fresh deadline, `script_rewrites += 1`, `action` cleared. The judge wrote
    the script, so no arming session follows. At the cap it is treated as `escalate`.
  * `confirm`: the existing hand-off unchanged — investigation on the PR's work order, or
    an improvement order for a hand-opened PR, through the steward as origin `morpheus`
    (needs `os.autonomy.self_dispatch`); `action` = `investigation:<id>` /
    `improvement:<io-id>`. The judge's `reason` goes into the hand-off text, marked as the
    monitoring agent's reading, not a diagnosis. If the steward declines, or the order
    has no PR (`pr_number` NULL — a user or investigation monitor): escalate.
  * `escalate`: the existing Neo `validation` question (`_ask_neo`), with the judge's
    reason added; `action` = `neo:<qid>`. Neo's `requeue` verdict and its cap stay.
* **A judge that fails** (None, kill, timeout): release the claim (`action` back to empty),
  `attempts += 1` on the row; after `MAX_OFFERS` failed judgings, `escalate` without a
  verdict — the question says the judge could not answer. Never a synthesised verdict.
* **Config:** `on_failed`/`on_timeout` keep parsing; they now choose the `confirm` route
  (`investigate`) and the fallback (`neo`), documented in place.
* **Invariant** `INV-MORPHEUS-OVERDUE` also fires on a `judge:<ts>` claim older than
  `turn_timeout_seconds * 3` (a judge that died holding its claim) and repairs it by
  clearing the claim.
* **Tests:** each verdict's transition and `action`; cap on rewrite; a None reply leaves
  `action == ""` and `attempts + 1`; a row PRE-claimed with `judge:<ts>` gets zero calls;
  a `cancelled` row seeded with empty `action` gets zero calls; `validated` never opens a
  session; a PR-less `confirm` escalates; a stale claim trips `INV-MORPHEUS-OVERDUE`.

Does NOT touch arming, the CLI or the UI.

## 5. The `jarvis mo` CLI and the ops it calls

Owns `cli.cmd_morpheus` (~4257) and `_morpheus_activity_text` (~4387), a new `mo`
subparser, and the `ops` functions it calls (`ops.mo_create`, `ops.mo_list`,
`ops.mo_show`, `ops.mo_cancel`, next to `ops.morpheus_report`/`morpheus_detail`).

* `jarvis mo create <project> "title" (--prompt "…" | --script FILE) [--every 15m]
  [--deadline 3d]` → `morpheus.create(origin="user")`; prints the id and "armed by the
  daemon on its next pass". It does not run a session itself.
* `jarvis mo list [project] [--all] [--state s]`, `show <id> [--json]`, `cancel <id>`.
* Every existing `jarvis morpheus` verb is available under `jarvis mo`: `status`, `add`,
  `enqueue-release`, `script`, `check`, `waive`, `requeue`, `rewrite`, `confirm`, `pause`,
  `unpause`, `follow-fleet`. `jarvis morpheus` stays a PERMANENT alias of `jarvis mo`
  (`scripts/shipit.sh` ~625 calls `jarvis morpheus enqueue-release` on whatever binary
  production runs). Verbs accept `mv-` and `mo-` ids via `is_monitoring_order_id`.
* `status` prints counts, pause, activity (§1.3 shape) — no session lines.
* **Tests** (`tests/test_cli*.py` / `tests/test_morpheus.py`): create with prompt, with
  script, with both or neither (refused); alias parity (`jarvis morpheus status` ==
  `jarvis mo status`); cancel of a terminal order refused.

Does NOT touch daemon passes, the dashboard or cost.

## 6. Dashboard

Owns `ui/app.py` routes for Morpheus (~817, ~2190-2285), templates `morpheus.html`,
`morpheus_item.html`, `_morpheus.html`, `_release.html`, and the nav entry in `base.html`
(~411).

* `/mo` (list) and `/mo/<id>` (detail) replace `/morpheus` and `/morpheus/<id>`; the old
  paths redirect (301) so links in inbox rows and Telegram keep working. `/go/mv-*` and
  `/go/mo-*` land on `/mo/<id>` (`ops.resolve_ref` gains `mo` in `_REF_ID`).
* List columns gain `origin`, `title` and frequency; a "New monitor" form POSTs to
  `ops.mo_create` and each open row gets "Cancel" (POST `ops.mo_cancel`). Every POST
  delegates to ops.
* The page header shows `report()` per §1.3: no session panel; activity and pause stay.
* Detail shows `last_session_id` and the order's cost (`bill.for_monitoring_order`, §7 —
  render the line only if that function exists, so this section does not wait on §7).
* Keep kn-abbf5b56's macros (`page_head`, `needs_you`, `filter_bar`, `row_id`, `explain`).
* **Tests** (`tests/test_morpheus.py`, `tests/test_ui_navigation.py`,
  `tests/test_release_console.py`, `tests/test_steward_queue.py` — all pin `/morpheus/`
  links today): both paths render; redirects asserted with `follow_redirects=False`
  (status 301 and `location`); `/go/mo-…`; create and cancel POSTs.

Does NOT touch daemon logic, CLI or search.

## 7. Cost, search, references, inspect and the docs

Owns `bill.py`, `cli.cmd_cost` (~3202), `search.py`, `ops._resolve_report_target`
(~13956), `notify.check_wo_link`, `onboard_prose.py` (~584, "costs one sonnet subagent"),
`CLAUDE.md`'s crib sheet and `tests/test_persona.py`'s slices, `docs/os/operator/*`,
`docs/os/index.md`, and a dated note at the top of the 2026-10-21 Morpheus spec pointing
here.

* **`bill.for_monitoring_order(mo_id)`** = the order's `agent_calls` rows
  (`central.agent_calls(wo_id=mo_id)`) folded by `_call_items`, no turn rows, never sealed.
  `bill.build` tries it when `is_monitoring_order_id`; `jarvis cost <mo-id>` prints it.
* **`jarvis cost --release <tag>`**: every MO with that `release_tag`, summed, with the
  count — the success-criterion number ("intake cost = O(new PRs)"). The pre-feature
  baseline is the `morpheus` line of `jarvis cost --fleet --window` around the previous
  release; print both in the PR's validation section.
* **Fleet totals** need no change: the per-kind line comes from `fleetcost.os_by_kind`,
  which counts agent calls by kind regardless of `wo_id`; pin with a test that an MO call
  appears once under `morpheus` there.
* **Search:** `search.KINDS` gains `monitoring` (central branch modelled on backlog):
  title, prompt, hints, resolution indexed. `tests/test_search.py` asserts
  `seen == set(search.KINDS)`: add an MO fixture there.
* **inspect** `<mo-id>`: reads `last_session_id`'s transcript (persisted, §3); with none,
  says "no transcript; see jarvis cost <id>".
* **`notify.check_wo_link`**: an MO id resolves to `/mo/<id>` before the
  `is_feature_order_id` check.
* **Crib + docs:** one crib line replacing the `jarvis morpheus …` line:
  `jarvis mo create <project> "title" --prompt "…"|--script f [--every 15m] [--deadline 3d]`
  and `jarvis mo list|show|cancel|waive|requeue|rewrite|confirm / pause|unpause|follow-fleet`
  (`jarvis morpheus` = alias). Keep the operator content dominant
  (`evals/llm/test_jarvis_judgment.py` loads CLAUDE.md as a prompt).
* **Tests:** an MO with two calls of DIFFERENT cost totals their sum; `--release` with a
  second tag present sums only its own; search finds an MO by title; `check_wo_link` for
  `mv-`/`mo-`; inspect's no-transcript message. The real before/after release number is a
  post-release check, not this PR's acceptance.

Does NOT touch daemon passes or the session runner.

## 8. Out of scope, rejected alternatives, rollout

* **Out:** investigations and the steward CREATING MOs themselves (the API `create(origin=…)`
  exists after §2; wiring those producers is backlog). Migrating per-project `queue_merges`
  semantics. Model calls inside check scripts.
* **Rejected — MO as a `feature_orders` kind:** per-project, one full worker briefing per
  session, FO statuses do not fit, and `is_feature_order_id` gates ~40 sites.
* **Rejected — renumber `mv-` to `mo-`:** ids are stored by equality across three DBs;
  a prefix-swap resolver would sit at every site.
* **Rejected — one batched short session per release with a subagent per PR:** cheaper by
  one system prompt per PR, but cost attribution per order would need splitting a session,
  and one order's failure would fail the batch. Sequential per-order sessions get most of
  the prompt-cache saving anyway.
* **Rejected — the session calling `jarvis mo script`:** it would need the production
  `JARVIS_HOME`; returning the script in its reply cannot write the wrong home.
* **Rollout:** §3 and §4 land behind nothing — they replace code paths. Order: §2; then §3;
  then §4, §5 and §6 in parallel (§5/§6 render §3's `report()` shape and share files with
  its minimal render edits); §7 after §5. The before/after number is taken on
  the first real release after §3 ships (`jarvis cost --release <tag>` vs the previous
  release's fleet window).

## Agent profile

You are a Jarvis OS developer working on ONE section of the monitoring-orders feature, in
the dev checkout of `agentic_os` (Python, stdlib-only core: argparse + sqlite3 + json; a
FastAPI/Jinja dashboard under `src/jarvis/ui/`). You implement your section of
`docs/superpowers/specs/2026-11-01-monitoring-orders.md` and nothing else. Section 1.3 is
the contract every sibling relies on: never change its names, signatures, states or reply
shapes; if your section cannot be built against it, ask Neo (`jarvis wo ask`) instead of
bending it.

What you must know about this codebase:
* Read the Serena memories first (`codebase-map`, `testing`), and navigate code with
  Serena's symbol tools (`find_symbol`, `find_referencing_symbols`,
  `get_symbols_overview`); the hooks refuse grep/sed over Python. Line numbers in the spec
  are approximate; the symbol names are exact.
* Morpheus lives in `src/jarvis/morpheus.py`; its rows in `os.db` via `CentralStore`
  (`src/jarvis/central_store.py`). No module opens SQLite directly; go through the store.
  Never poke a database by hand.
* Every model call the OS makes goes through `src/jarvis/claude_cli.py` and is recorded
  through `agent_usage.record`. Tests never touch the real `claude`: use the `call=`
  injection seams and the fixtures in `src/jarvis/testing.py`.
* The CLI imports lazily inside handlers; the UI's POSTs delegate to `ops`; nothing in the
  UI imports `morpheus.STATES` (read `report()['counts']`).
* `mv-` ids are permanent; new rows are `mo-`. Use `morpheus.is_monitoring_order_id`,
  never `startswith("mo-")`, and never add `mo-` to `project_store.is_feature_order_id`.

Rules you must follow:
* Never fabricate a verdict, script or answer from a failed model call (kn-40db1828): the
  order stays owed and retryable, bounded by a counter on the row.
* Every exemption from a counter (kill, usage limit) needs its own bound (kn-1e43fe1a).
* Run only the targeted tests for what you changed (`uv run pytest tests/<file> -q`);
  never the full suite locally. When you add a state, kind or column, grep `tests/` for the
  literal string and the tuple names (kn-97fc3024): exact-tuple pins live in files you did
  not touch.
* Write a test for the vacuous version of each acceptance check — one that would pass for
  the wrong reason today — and make sure yours does not.
* Keep `jarvis morpheus` working: production's `shipit` calls
  `jarvis morpheus enqueue-release`.
* PR body needs a `## How to validate in production` section (the Morpheus hints). Keep it
  concrete: commands and the exit codes they should give.
* Follow the house style (caveman/ponytail): smallest change that fully solves your
  section; no options or abstractions nobody asked for; file deferred work with
  `jarvis backlog add jarvis_os "…"`.

Never: edit another section's files beyond what your section lists; resume a Claude
session from monitoring code (no `--resume`, no long-lived state); let a session write
into `JARVIS_HOME`; re-arm an order already `monitoring`; close a GitHub issue by hand;
write under `~/workspace/production`.
