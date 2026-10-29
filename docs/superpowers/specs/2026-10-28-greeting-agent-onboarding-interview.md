# The greeting agent: an interview that onboards a project with the configuration the user chose

`jarvis adopt <path>` prepares a project's FILES — OPERATION.md, `.claude/settings.json`,
`.gitignore`, `.jarvis/`, the project skills, workspace trust — and nothing else. It never
touches the catalog document, so a freshly adopted project runs on every shipped default:
validation off, supervisor off, rules off, fleet health off, the schedule off, evolution in
shadow, Morpheus on, self-dispatch off. Nobody chose any of that and nobody was told.

This feature adds the missing half. `jarvis onboard start <path>` opens an interactive Claude
session in the user's own terminal, run by a new persona, `greeting_agent`. It interviews
the user about every automatic behaviour of the OS, explains the impact of any choice with
worked examples grounded in the specs and in the config code, shows a diff, and applies
the result as ONE configuration version plus a bootstrap — so the project is onboarded
with exactly the configuration the user chose.

## 0. What already exists, and the premise that is wrong

All of this was verified in the worktree. Do not re-derive it.

- **The config console is landed.** `ops.set_config` / `unset_config` / `config_get` /
  `config_show` / `config_history` / `config_diff` / `restore_config` / `adopt_config`
  (`src/jarvis/ops.py`, from line 10935), spec
  `docs/superpowers/specs/2026-08-27-the-config-console.md`. `os_config_versions` in
  `os.db` is the record, the catalog file is a materialised view, and
  `ops._commit_document` (ops.py:10739) is the only writer: validate, write the file
  atomically, record the row, in that order. Head is read through
  `CentralStore.head_config_version()`, never off `config_versions()[0]`.
- **`config_version.resolve(catalog)`** (`config_version.py:90`) flattens a parsed catalog
  to `dotted path -> value` with every default materialised. It IS the schema. A minimal
  one-project catalog yields **374 paths: 202 under `os.`, 172 under `projects.<name>.`**.
  `config_version.diff(a, b)` compares two such maps.
- **`ops.apply_class(path)`** returns `hot` | `next-dispatch` | `restart` with
  `ops.APPLY_NOTES`; **`ops.safety_key(path)`** over `catalog.SAFETY_KEYS` marks the keys
  that change what a worker is ALLOWED to do. Both are reused as-is; neither is restated.
- **`ops._key_path` refuses an unknown project**, so `jarvis config set` CANNOT create a
  `projects[]` entry. Onboarding needs its own write path (§3) and cannot be assembled out
  of `jarvis config set` calls.
- **`ops._refuse_worker_write`** (ops.py:10679) refuses every config write when
  `JARVIS_WO_ID` is set.
- **`hooks.gate_decision` returns `None` when `JARVIS_WO_ID` is unset** — "gates govern
  dispatched workers only" — and `hooks.preflight_decision` then auto-allows any
  `jarvis …` chain. An interactive greeting session can call the CLI freely; a dispatched
  worker cannot. That is correct as it stands; do not "fix" it.
- **A lead session's persona is `--agent <name>` plus `--add-dir <root>`**, where `root`
  contains `.claude/agents/<name>.md`: `claude_cli._briefing_args:338` ("The LEAD
  session's persona, not a subagent type"), `specs.agent_root:33` ("The directory handed
  to `--add-dir` so `--add-dir/.claude/agents/` is found"), written by
  `specs.install_agent:81`, rendered by `specs.render_agent:67`. Persona files are
  frontmatter (`name`, `description`, optional `tools`) plus prose — see
  `assets/agents/*.md`, `assets/worker-agents/*.md`, `assets/neo-seats/*.md`.
- **`spec_index.section(markdown_text, which)`** (spec_index.py:48, over
  `sections.extract_section`) returns one section of a markdown document by number or
  heading. `spec_index.toc` and `spec_index.search` are its siblings. This is the
  grounding mechanism of §4.
- **`bootstrap.bootstrap_project(spec)`** does the file half, and `ops.start_os`
  (ops.py:142-192) is the ONLY place that follows it with
  `CentralStore.upsert_project(...)`.

**The premise to discard.** The feature order says `src/jarvis/bootstrap.py` "already runs
an interview session to negotiate the per-project launcher contract (wo-036e8be2, PR #32)"
and should be reused. It does not. That work is commit `61797aac` on the **unmerged**
branch `worktree-wo-036e8be2`, which added `src/jarvis/onboarding.py`, `launcher.py` and a
`kind=bootstrap` work order; none of it is on `main`.
`project_store.WO_KINDS = ("worker", "planner", "manager", "analyst", "investigator")` —
no bootstrap kind, no `onboarding.py`, no `jarvis onboard`. The launcher idea landed in a
different shape as `session_dispatch.parse_launcher` (#1239).

Two consequences, which are decisions and not observations:

1. **A greeting session is NOT a work order.** It is a foreground `claude` process in the
   user's terminal with a human in the room: no branch, no PR, no assumptions, no turn
   accounting. Nothing here touches `WO_KINDS`, `dispatch.py` or the daemon.
2. **Launcher-contract negotiation is out of scope** and is filed to the backlog. This
   feature onboards CONFIGURATION. A project needing somebody else's session wrapper still
   gets it through `session_dispatch.parse_launcher`.

### 0.1 Non-goals, written down so a cold worker does not invent them

No new `WO_KINDS` entry and no work-order kind of any sort. No `hooks.py` change. No new
invariant — `INV-CONFIG-DRIFT` (`invariants.py:3131`) already compares the catalog file
against the ledger head by document, and going through `ops._commit_document` satisfies it.
No UI change: the `/config` tab (`ui/app.py:1654`, `config_scope_of:765`,
`config_tree:778`, `config_rows:867`, `config_widget:811`, `config_type_ok:840`) keeps its
own grouping, and this feature's areas do NOT replace `config_tree`. No daemon hot-add of
a project (see §3). No new page under `docs/os/`. No reviving `onboarding.py` or
`launcher.py` from the unmerged branch.

### 0.2 The surface, whole, so no section invents a neighbouring command

```bash
jarvis onboard start <path> [--name n] [--print-prompt]   # §5 — open the interview
jarvis onboard questions [--area a] [--json]          # §4 — the catalogue, no model needed
jarvis onboard explain <area|path> [--project p]      # §4 — impact, example, apply class
jarvis onboard apply --from <draft.json> [--dry-run]  # §3 — the one validated write
```

`jarvis adopt` keeps working unchanged and `jarvis onboard apply` calls into it; `adopt`
gains one line of help text pointing at `onboard` (§5). The draft file is the only state
the interview carries between turns, and the CLI is the only thing that writes
configuration — the persona never edits JSON (prime directive 1).

**Three sections add a subcommand to the same `onboard` group, so the rule is: whichever
lands first CREATES the group in `cli.build_parser`, the rest ADD to the existing one.**
§3 lands first and creates it; §4 adds `questions` and `explain`; §5 adds `start`. Without
that rule the last two pull requests conflict in `build_parser` over a group they both
think they own. When the group is complete its subcommands are exactly `start`,
`questions`, `explain`, `apply` — §5 pins that set, so a fifth command cannot appear
without this section being updated first.

**The entry point is a NAMED subcommand, `jarvis onboard start <path>`, not a bare
`jarvis onboard <path>`.** Once an argparse subparser group carries choices, a bare path is
refused as an invalid choice, so the two cannot coexist in the shape this repo uses
everywhere (`add_subparsers(dest=...)` with `required=True`). The path is `start`'s
positional argument.

### 0.3 The draft file, frozen here because two children read and write it and never speak

```json
{
  "schema": 1,
  "project": {"name": "shared_schedule",
              "path": "/home/gonzalo/workspace/shared_schedule",
              "description": "", "model": ""},
  "reason": "I want reviews on but nothing dispatching itself yet",
  "values": {"projects.shared_schedule.validation.enabled": true,
             "os.neo.enabled": true},
  "answers": [{"area": "validation", "mode": "preset:balanced"},
              {"area": "evolution", "mode": "defaults"}]
}
```

- `schema` is `1`; an unknown value is refused by both readers.
- `values` keys are **resolved paths spelled exactly as `config_version.resolve` spells
  them**, so §3 can hand each one straight to `ops._document_slot`. Values are the catalog's
  own types (a list for a roster, a float for a share, `null` where the catalog means null).
- `project.name` / `path` / `description` / `model` are first-class and are NOT in
  `values`: `config_version._SKIP[ProjectSpec] = {"raw", "name"}` means a project's name
  never appears in the resolved map at all, and `projects.*.path` is `restart` class
  (`ops.APPLY_RULES`, ops.py:10616).
- `answers` is provenance for the summary and the re-run; §3 ignores it entirely.
- `reason` is **mandatory in practice**, and that is not a style preference:
  `catalog.SAFETY_KEYS` (catalog.py:27-51) covers `*.permission_mode`, `*.gates.*`,
  `*.validation.*`, `*.schedule.*`, `*.rules.*`, `*.supervisor.remedies.*`,
  `os.neo.enabled`, `os.supervisor.enabled`, `os.supervisor.health_enabled`,
  `os.autonomy.*` and the `os.evolution.*` money and depth keys — which is most of what
  this interview is FOR. `ops._require_reason` (ops.py:10885) refuses a write that moves
  one of those without a reason. One reason for the whole onboarding is enough, and the
  interview asks the user for it in their own words (§2) rather than synthesising
  "onboarding interview".

**Both sections that touch the draft test against the block above, not against each
other.** §2 and §3 never speak, so each extracts this section's fenced JSON at test time —
`spec_index.section(Path("docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md").read_text(), "0.3")`,
take the fenced block, `json.loads` it — and asserts against that literal. §2 writes it to
disk byte for byte and loads it back, and round-trips it so the top-level keys are exactly
`schema`, `project`, `values`, `answers`, `reason` with `project` exactly `name`, `path`,
`description`, `model`. §3 writes the SAME literal with a plain `json.dump` — deliberately
not through this feature's own `save` — and asserts `ops.onboard_apply` accepts it, which
is what proves it reads the on-disk shape rather than an object a sibling handed it. Both
assert that `"schema": 2` is refused with a message containing the word `schema`. Coupling
both suites to this text is the point: whichever half drifts goes red against a file
neither section owns.

### 0.4 The test trap every section inherits

`tests/` is routinely run BY a worker, which inherits `JARVIS_WO_ID`, and
`ops._refuse_worker_write` reads exactly that — so every config write in every test fails
with the worker lockout unless the file carries the autouse fixture that deletes the
variable. `tests/test_config_console.py:23` (`not_a_worker`) has the one to copy (learning
`kn-650b6f24`). Do not scrub the variable globally; `testing._bills_real_tokens` needs it.
Every test file in §2, §3 and §4 carries that fixture **plus one line asserting
`os.environ.get("JARVIS_WO_ID") is None` during the run**, so a missing fixture fails as
itself rather than as a confusing worker-lockout refusal thirty assertions later.

Run only the tests covering what you changed. CI runs the suite on three Python versions,
which is strictly more than a local full run (`kn-356c724b`).

## 1. The registry: every setting classified into a behaviour area

**New module `src/jarvis/onboard_knobs.py`.** Code only, deliberately small, and the thing
three other sections import. It classifies; it holds no prose and no recommended values.

```python
AREA_KEYS: tuple[str, ...]            # the interview's order: must-haves first
MUST_HAVE: frozenset[str]             # the areas the user named; every one precedes the rest
AREA_PREFIXES: dict[str, tuple[str, ...]]   # area key -> the resolved-path tails it owns
AREA_SCOPE: dict[str, str]            # "os" | "project" | "both"
UNASKABLE: frozenset[str]             # identity/plumbing tails, never asked

@dataclass(frozen=True)
class Area:            # declared here, FILLED in §4
    key: str
    title: str
    scope: str
    summary: str       # what this behaviour does when nobody asks it to
    on_costs: str      # what turning it on costs, in money and in attention
    off_costs: str     # what staying off costs
    refs: tuple[tuple[str, str], ...]     # (spec or doc path, section number or heading)
    presets: dict[str, dict[str, Any]]    # posture -> {path tail: value}

def area_of(path: str) -> tuple[str, str] | None:   # (area key, path tail)
def asked(path: str) -> bool
def coverage(resolved: dict[str, Any]) -> list[str]  # every path it cannot classify
```

- The registry keys on the **path tail**, not the full path: `os.validation.enabled` and
  `projects.p.validation.enabled` are the same decision asked at two scopes, and ~210
  unique tails cover all 374 paths.
- **`area_of` is not pure prefix matching on the `os.` side.**
  `config_version._RENAMES[catalog.OsConfig]` maps `default_model -> defaults.model`,
  `default_max_concurrent -> defaults.max_concurrent`, `max_in_flight ->
  defaults.max_in_flight` and so on, so `os.defaults.model` and `projects.X.model` are one
  knob under two spellings and must land in the same area. Handle the rename table
  explicitly rather than hoping prefixes line up.
- **Areas.** One per automatic behaviour, and each of these must be its own area because
  the user named it: self-evolution (`evolution.*`), panel validation (`validation.*`),
  Neo and its panel (`os.neo.*`, `os.neo.panel.*`), the self-healing rules registry
  (`rules.*`), supervisor health, probes and remedies (`supervisor.*`,
  `supervisor.probes`, `supervisor.remedies.*`), fleet health (`fleet_health.*`),
  Morpheus production validation (`os.morpheus.*`), steward spend and self-dispatch
  (`os.autonomy.self_dispatch`, `evolution.weekly_usd`, `evolution.critical_weekly_usd`,
  `evolution.order_budget_usd`, `evolution.yield_above_usd`, `evolution.max_depth`),
  privileged-action gates (`gates.*`), budgets and concurrency (`worker.budget_usd`,
  `worker.feature_budget_usd`, `max_concurrent`, `os.defaults.max_in_flight`), model,
  effort and permission mode (`model`, `worker.model`, `worker.effort`,
  `worker.permission_mode`, `worker.transport`), investigation and improvement orders
  (`worker.investigation_model`, `worker.investigation_effort`,
  `worker.investigation_budget_usd`, `os.neo.review_findings`), compaction and caching
  (`worker.autocompact_window`, `worker.cache_ttl`, `os.compact_min_context`,
  `os.cold_prefix_floor`, `os.cold_prefix_floor_max`, `os.cache_health_*`), navigation
  (`navigation.*`, `worker.py_nav_hook`, `worker.doc_nav_hook`, `worker.bash_first`,
  `worker.tool_search`, `worker.doc_read_limit_lines`), cost and metering (`cost.*`),
  inspection alarms (`inspect.*`), concision (`concision.*`), wiring (`wiring.*`),
  worktrees (`worktree.*`), the schedule (`schedule.*`), the dashboard's self-healing
  (`ui_health.*`), observability (`observability.level`), messaging (`messaging.*`), bugs,
  pull requests and releases (`bugs.*`, `pull_requests.*`, `release.*`), knowledge
  injection (`os.knowledge_*`), notifications and the dashboard (`os.notifications.*`,
  `os.ui.*`), and the worker environment leftovers (`worker.denied_tools`,
  `worker.mcp_tool_timeout_ms`, `worker.require_crew`, `worker.append_system_prompt`,
  `os.disk_free_warn_gb`, `os.max_os_prompt_chars`). Investigation/improvement orders have
  no enable knob of their own; the area exists anyway, because the user asked to be told
  about them, and §4's prose says plainly that they are always available.
- **`asked` is a rule, not a list.** A path is asked when it is a scalar or a short enum
  the user can hold an opinion about — an `enabled` flag, a mode, a model, a money
  ceiling, a concurrency limit. A path is not asked when it is an alarm threshold, a char
  or minute budget, a roster, or a dict/list that `config_version._flatten` emits as one
  value (`fleet_health.thresholds`, `supervisor.probes`, `navigation.*` patterns,
  `validation.seat_models`, `gates.patterns`). Expect roughly 40-60 asked tails; the
  number falls out of the rule rather than being decided.
- **`UNASKABLE`** is for identity and plumbing: `path`, `description`,
  `settings_overrides`, `worker.launcher`, `os.ui.port`, `os.ui.base_url`,
  `os.notifications.telegram.token_env`, `os.notifications.telegram.chat_id_env`. They are
  still classified into an area. **There is no exemption from classification** — that is
  the whole guarantee of this section.

- **`MUST_HAVE`** is the areas the user named by name, and it is a symbol rather than
  prose because §2's `accept_remaining` and §5's persona both need to know when the
  must-haves are done; left to prose, each would invent its own list. Every must-have key
  precedes every other key in `AREA_KEYS`.

**Tests — `tests/test_onboard_knobs.py`.** Build the catalog document as a literal dict in
the test file, `catalog.parse_catalog` it, `config_version.resolve` it, and then:

1. **Pin the fixture to the real schema** before asserting anything about coverage:
   `len(resolved) == 374`, 202 paths under `os.`, 172 under `projects.`. Without this,
   an empty `coverage()` proves nothing — and the counts are also the guard that stops a
   worker making coverage easier by editing the resolver, which is read-only here.
2. `coverage(resolved)` is empty: a knob added to `catalog.py` without a registry entry
   fails here, which is the point.
3. **The classification is not one bucket.** `len(AREA_KEYS) >= 25`; every key in
   `AREA_KEYS` is returned by `area_of` for at least one path in the resolved map, so no
   area is decorative; no single area claims more than a quarter of the tails. One area
   with the prefix `""` would otherwise satisfy everything above it.
4. **`asked` says yes to something.** The tails for which `asked` is true number between
   40 and 80, and `asked` is true for `projects.p.validation.enabled`, `os.neo.enabled`
   and `projects.p.worker.budget_usd`. `def asked(path): return False` passes every other
   assertion in this section and leaves §2 and §4 with nothing to ask about.
5. `asked` is false for every dict- or list-valued path in the resolved map, and for every
   `UNASKABLE` tail — which still classifies.
6. `area_of` returns the same area for `os.defaults.model` and `projects.p.model` (the
   rename trap).
7. **The `Area` contract, with dummy content built in the test file** so §4 is not
   imported: its field names and their order are exactly `key`, `title`, `scope`,
   `summary`, `on_costs`, `off_costs`, `refs`, `presets`; assigning to one raises
   `dataclasses.FrozenInstanceError`. `AREA_SCOPE`'s keys equal `set(AREA_KEYS)` and its
   values are all in `{"os", "project", "both"}`.
8. `MUST_HAVE` is non-empty, a proper subset of `AREA_KEYS`, and ordered ahead of the rest.

**This file asserts nothing about prose or presets** — those are §4's, and asserting them
here would make this section's suite red until §4 merges, which is a false dependency in
the wrong direction. `catalog.py` and `config_version.py` are read-only for this section;
run `tests/test_config_console.py` unchanged and green and say so in the pull request.

## 2. The interview: questions, postures, the two scopes, and the re-run

**New module `src/jarvis/interview.py`** — pure: no model, no process, no write outside
the draft file. Everything done to an answer happens here, which is how the flow is
testable without a live model.

**What this section does NOT own: the draft file itself.** `onboard_draft.load` / `save` /
`candidate_document` / `validate` ship with §3, because the draft's schema and the
document it becomes are the write path's contract. This section consumes them.

- `new_draft(path, name=None, current=None)` builds a `onboard_draft.Draft` and saves it.
  On a **re-run** of an adopted project,
  `current` is `ops.config_show(project)["resolved"]`: `answers` carries, per area, the
  posture whose preset matches the live values exactly (`match_posture`, else `"custom"`),
  and `values` carries what the document actually sets, so the interview revises rather
  than resets. That is the whole of the re-run requirement — the same flow with a
  different starting point, not a second command.
- `questions(draft, areas)` returns the unanswered areas in `AREA_KEYS` order. A
  `Question` quotes the area (`title`, `summary`, `on_costs`, `off_costs`), names its
  enable tail and current value, and lists the postures available with what each would
  set. It carries **no prose of its own**.
- `answer(draft, area_key, "preset:<posture>")` expands a posture from `Area.presets`
  into `values` at the right scope. `set_value(draft, path, value)` is the per-knob escape
  hatch and marks the area `"custom"`. `accept_defaults(draft, area_key)` records the area
  as answered and writes **no values at all** — the shipped default stands and the
  document stays silent about it, which is what keeps a default-accepting onboarding from
  freezing today's defaults into the catalog forever. `accept_remaining(draft)` does that
  for every unanswered area.
- `set_reason(draft, text)` records the user's own sentence (§0.3).
- **The two scopes, and the hole this closes.** For the first project, answering `os.*`
  areas (Neo, Morpheus, autonomy, steward spend, defaults, compaction, knowledge) is
  exactly right. For the second project onto a live fleet, the same answers silently
  reconfigure every other project. So: when the catalog already holds projects, every area
  whose `scope` is `"os"` starts at "leave the fleet setting as it is", and an `os.`-scoped
  value may only enter `values` after `fleet_warning(draft)` has been surfaced — the
  question carries the warning text and names the other projects it would reach.
  `project`-scoped areas are unaffected. For an area with scope `"both"`, the interview
  writes the project spelling, never the `os.` one, unless the user explicitly asks for
  fleet-wide.
- **The one area that is not a resolved-value question: wiring.** Only four `wiring.*`
  paths exist, but the real choice space is the user's actual MCP servers, skills and
  plugins, discovered at runtime. The interview special-cases it by calling
  `ops.wiring_config(project=…)` / `ops.wiring_show(…)` (ops.py:11054) and emitting
  `wiring.*` values through `wiring.lever_setting(lever, wired, cfg)`, which returns the
  `(path, value)` pair to put in the draft. **Do not call `ops.set_wiring`**: it routes
  through `ops.set_config`, which refuses a project that is not in the document yet —
  which, during onboarding, is every project.
- **`ops._refuse_os_sweep_off`** (ops.py:10770) raises when a write would leave
  `supervisor.enabled` or `supervisor.health_enabled` off for the OS-owning project
  (`schedule.os_project`). The interview must not OFFER that choice when onboarding or
  re-onboarding that project; §3 raises anyway as the backstop.
- Every answer is checked through `onboard_draft.validate(draft, document)` before it is
  saved, so a posture that produces an unparseable document is caught here — it is a bug
  in §4's presets — rather than at the user or at apply time. The catalog's own parser is
  the type system; the interview needs none of its own.
- `summary(draft, document) -> list[dict]` is `config_version.diff(resolved_before,
  resolved_after)` over the real resolved maps, each change annotated with
  `ops.apply_class(path)`, `ops.safety_key(path)` and the knob's own line from §4. A real
  resolved diff cannot drift from what the fleet will read; a hand-rolled one can.

**Tests — `tests/test_interview.py`** (with the §0.4 fixture and its guard line).
**Preset fixtures in this file are the test's own**, not §4's corpus, so this section is
not blocked on it — and they deliberately include a posture that would turn the supervisor
sweep off, so assertion 3 exercises filtering code rather than an empty loop.

1. `accept_defaults` writes no values and leaves the document silent.
2. **The fleet warning is enforced, not displayed.** With a second project in the catalog,
   `answer(draft, <an os-scoped area>, "preset:balanced")` before any `fleet_warning` call
   either raises or writes zero `os.` keys into `values`; after `fleet_warning(draft)` the
   same call writes them. Without the negative half the rule is decorative.
3. **The OS project's sweep is never offered.** With a catalog whose `schedule.os_project`
   is the project being onboarded, no posture in any `Question` from `questions()` sets
   `supervisor.enabled` or `supervisor.health_enabled` false.
4. **`summary` equals the real diff.** Its rows equal `config_version.diff(before, after)`
   computed independently in the test from `resolve(parse_catalog(...))`, and for each row
   `row["apply"] == ops.apply_class(row["path"])` and
   `row["safety"] == ops.safety_key(row["path"])`. "Reports only the project's creation"
   would pass on a stub returning one hard-coded row.
5. **The parser is the type system.** `set_value` with a value `catalog.parse_catalog`
   rejects comes back from `onboard_draft.validate` carrying the real `CatalogError` text —
   assert a substring the test obtains by calling `parse_catalog` on the same bad document.
   A worker who writes a second type check passes a looser assertion and fails this one.
6. A re-run draft built from a live resolved map pre-fills the matching posture, and
   `"custom"` where nothing matches.
7. **The repository stays clean.** After `new_draft` and a save, the project directory's
   file list is exactly what it was, the draft path is under `paths.jarvis_home()`, and no
   temporary sibling is left beside it.
8. The §0.3 round-trip and the `"schema": 2` refusal that section describes.

## 3. The draft file and `jarvis onboard apply`: one config version, then bootstrap, then the restart

**New module `src/jarvis/onboard_draft.py`** — pure, stdlib plus `catalog`, and the
contract both halves of the feature read:

- `Draft` over the §0.3 schema, with `load(path)` / `save(draft, path)`. `save` is atomic
  (temp + `os.replace`); `load` refuses an unknown `schema`. Drafts live under
  `paths.onboarding_dir()` (new, `$JARVIS_HOME/onboarding/`) and NOT inside the project, so
  an abandoned interview leaves nothing in the user's repository.
- `candidate_document(draft, document)` returns what the catalog document would become —
  the `projects[]` entry created or updated from `draft.project`, plus every path in
  `values` placed through `ops._document_slot`. It **mutates nothing**.
- `validate(draft, document) -> list[str]` runs the candidate through
  `catalog.parse_catalog` and returns the `CatalogError` text as problems.

This module is here rather than in §2 because the draft's schema and the document it
becomes are the write path's contract, and because it lets this section land without
waiting for the interview. It knows nothing about questions, postures or areas.

**`ops.onboard_apply(draft_path, *, catalog_path=None, dry_run=False)`** is the only thing
in this feature that writes configuration, and the persona reaches it only through
`jarvis onboard apply --from <draft.json>`. The order of operations IS the decision:

1. `_refuse_worker_write("onboard apply")` first, exactly as `set_config` does.
2. `onboard_draft.load(draft_path)`. Refuse a draft whose `project.path` is not a directory
   or not a git repository (name `git init` in the message, as `bootstrap_project` does),
   or whose `project.name` already names a DIFFERENT path in the document.
3. `document = _read_document(_catalog_file(catalog_path))`, `before = _resolved_of(...)`,
   `candidate = onboard_draft.candidate_document(draft, document)`.
4. `problems = onboard_draft.validate(draft, document)` — refuse with every problem at once.
5. `after = _resolved_of(candidate, file)`; `_refuse_os_sweep_off` for each changed key;
   `_require_reason(changes, draft.reason, retry)` so the safety-key rule is the console's
   and not a second implementation; then **`_commit_document(candidate, path=file,
   actor="onboard", reason=draft.reason, changes=config_version.diff(before, after))`**.
   **ONE version row for the whole interview**, not one per answer: the user made one
   decision about one project, and forty rows would make `jarvis config history` useless.
6. `bootstrap.bootstrap_project(spec)` where `spec` is read back out of the parsed
   candidate catalog — never hand-built — so the files are bootstrapped from exactly what
   was committed.
7. `CentralStore.upsert_project(name, path, description, model,
   catalog_json=json.dumps(spec.raw))`, mirroring `ops.start_os`. A catalog entry with no
   `projects` row in `os.db` is a project `jarvis status` and the dashboard cannot see.
8. Return `{"version": row, "changed": bool, "changes": [...], "bootstrap": report,
   "apply": {path: class}, "restart_needed": bool, "restart_why": str}`.

**The restart is the biggest fact in this feature, and this section owns saying so.**
`Daemon.reload_catalog` (daemon.py:799-805) refuses a reload when the set of project names
changed: *"added or removed a project, and that is not a setting … Run `jarvis start` to
apply it. Setting changes in the same file are not applied either until you do."* So
onboarding a new project onto a running fleet means the project does not exist for the
daemon until a restart — and, worse, **every other setting change in the same file is
frozen until then**. `restart_needed` is true whenever the project is new or any changed
path's `ops.apply_class` is `restart`, and the CLI prints `jarvis start --catalog <file>`
as the next command in the success output, not as a footnote. Do not make the daemon
hot-add a project: that is its own feature, and it is filed to the backlog.

`--dry-run` stops after step 5's resolve and returns the same payload with no row and no
bootstrap; it is what the persona shows the user as the confirmation diff. Re-applying an
unchanged draft writes no new version — version ids are content-addressed, so this falls
out of `_commit_document` rather than needing a check — and a test pins it.

**The gate.** `gate_rules` gains `jarvis onboard apply` to the `config_write` recogniser
(`gate_rules.py:803`) with a canary beside the existing `config_write` ones
(`gate_rules.py:836`), because it changes the fleet's own configuration in exactly the
sense that gate kind exists for. This binds dispatched workers only; an interactive session
is unaffected (§0). The two refusals are deliberately asymmetric and belong in the same
change: the ops lockout cannot be bypassed, the gate explains itself.

**Tests — `tests/test_onboard_draft.py`.** A draft round-trips through `save`/`load`; an
unknown `schema` is refused; `candidate_document` leaves its input document untouched and
places both an `os.`-scoped and a `projects.<name>.`-scoped value where
`config_version.resolve` reads them back equal; `validate` returns the parser's own message
for a value the catalog rejects, and `[]` for a document that parses.

**Tests — `tests/test_onboard_apply.py`** (with the §0.4 fixture and its guard line).

1. A draft for a fresh git project creates the `projects[]` entry; `bootstrap_project` ran
   (OPERATION.md and `.claude/settings.json` exist); a draft naming a non-git path is
   refused naming `git init`; a draft whose safety keys move with an empty `reason` is
   refused by `_require_reason`; a worker session (`JARVIS_WO_ID` set) is refused.
2. **One row, and it is the head.** `CentralStore.config_versions()` grows by exactly one,
   and `head_config_version()["id"]` equals the returned row's id. Reading head off
   `config_versions()[0]` is the §0 ban and this is what catches it.
3. **The spec is read back, not hand-built.** With a non-default `model` and `description`
   in the draft, the committed `projects[]` entry carries both AND the bootstrapped files
   reflect the committed value. A hand-built `ProjectSpec` passes "bootstrap ran" and fails
   this.
4. **`upsert_project` mirrors `ops.start_os`.** The `os.db` row's `catalog_json` parses and
   its `name` is the draft's project. "The project is in os.db" passes with a null
   `catalog_json`, which is the bug step 7 exists to prevent.
5. **Order of operations.** With `bootstrap.bootstrap_project` monkeypatched to raise,
   `onboard_apply` propagates BUT the version row exists and the catalog file is the
   candidate document. That pins commit-before-bootstrap, which is this section's decision.
6. **The dry run tells the truth.** For one draft, the `changes` of a `dry_run=True` call
   equal those of the real call; the dry run leaves `config_versions()` the same length and
   the catalog file byte-identical. The persona shows the dry run as the confirmation diff,
   so a divergence there is a lie to the user.
7. **`restart_needed` both ways.** True with a `restart_why` naming the new project, for a
   new project; **false** for a draft against an already-present project that changes only
   a path whose `ops.apply_class` is `hot`. A constant passes the "true" half alone.
   Separately, the CLI success output contains the literal `jarvis start --catalog ` and
   the catalog path.
8. Re-applying the unchanged draft writes no second row.
9. **The gate canary in the existing idiom** (`tests/test_config_console.py:495`):
   `gate_rules.RuleSet.from_seeds().check_canaries()` is empty, and
   `jarvis onboard apply` classifies as `config_write`.

**Must not change.** `ops._commit_document`, `_require_reason`, `_refuse_os_sweep_off`,
`_refuse_worker_write`, `set_config` and `unset_config` keep their behaviour:
`tests/test_config_console.py` passes untouched and is named in the pull request body.
After an apply, `INV-CONFIG-DRIFT` reports nothing. Re-proving `Daemon.reload_catalog`'s
refusal end to end is NOT this section's job — it is covered where it lives; assert only
`restart_needed`, `restart_why` and the printed command.

## 4. The corpus and `explain`: what each behaviour costs, in words, grounded

**New module `src/jarvis/onboard_prose.py`.** It fills §1's `Area` dataclass, one entry per
`AREA_KEYS` key, and it owns **every word a user will read about what an automatic
behaviour does** plus the recommended values behind each posture. The rule that keeps this
out of its siblings: *if you are writing a sentence a user reads about what a behaviour
costs, or choosing a number to recommend, you are this section.*

- `AREAS: tuple[Area, ...]` — `title`, `summary` (what it does when nobody asks it to),
  `on_costs`, `off_costs`, `refs` and `presets`.
- `LINES: dict[str, str]` — one line per asked path tail: what this knob changes, in the
  user's terms. Plus `EXAMPLES: dict[str, str]` — a worked consequence for each asked tail,
  concrete the way the user asked for: *"with `os.autonomy.self_dispatch` on, a probe
  failure at 03:00 files an improvement order and dispatches it the same night, inside
  `os.evolution.weekly_usd` ($150/week by default); with it off the same failure waits in
  `jarvis inbox` until you read it."* **Every number comes from a resolved default or from
  a spec section, never from the model's guess.**
- **Postures** are `off`, `cautious`, `balanced`, `autonomous`; an area defines the subset
  that means anything for it. A posture's dict is path TAILS to values, spelled as the
  catalog spells them.
- **Grounding: the refs are spec sections, not `docs/os/` pages.** `docs/os/` covers
  orders, reviews, records, spend, the agent diagrams, testing, environments and
  concision — there is **no `docs/os/` page for Morpheus, evolution, steward spend,
  compaction, navigation, the schedule, autonomy, worktrees, cost, fleet health, UI health
  or inspection. So "grounded in docs/os/" is only half available, and the other half is
  `docs/superpowers/specs/*.md`, reachable mechanically through `spec_index`. A ref is
  `(path, section)` and may point at either tree. Do not write new `docs/os/` pages to
  close the gap: that is a different feature, and it is filed to the backlog.
- **Reuse the words that exist.** `ui/app.py:894 WIRING_GROUPS` already carries three good
  blurbs for the MCP / skills / plugins groups and `WIRING_EVIDENCE` carries the one
  measured claim; the wiring area quotes them rather than inventing a second wording.

**`ops.onboard_explain(target, *, project=None, catalog_path=None)`** is a thin renderer
over §1 plus this corpus, and it holds no words of its own. Given an area key it returns
the title, summary, on/off costs, the enable path and its current value, every posture with
the values it would set and each value's line, and the grounding excerpt
(`spec_index.section(Path(ref[0]).read_text(), ref[1])`, truncated with the path and
section named so the user can read the rest). Given a path or tail it returns the area it
belongs to, its line, its example, the current resolved value and the shipped default,
`ops.apply_class` with the matching `ops.APPLY_NOTES` text, `ops.safety_key`, and the exact
`jarvis config set` command that would change it later — so the user leaves the interview
knowing the configuration is not frozen. Current values come from
`ops.config_show(project)["resolved"]` when a project is given and from a default catalog
otherwise, because `explain` has to work before the project exists.

**`ops.onboard_questions(area=None)`** returns the whole catalogue — every area with its
prose, postures and knobs by tier. `jarvis onboard questions --json` is how a test, the
user or a future UI reads the interview without starting one.

**Tests — `tests/test_onboard_prose.py`** (with the §0.4 fixture and its guard line).

1. **Every ref resolves** — the file exists and `spec_index.section` returns something
   non-empty for its section. This is the strongest test in the feature: it is what keeps
   the explanations honest as the specs move. On top of it, every ref also parses as the
   command the persona actually runs: `cli.build_parser().parse_args(["spec", "section",
   <file>, <section>])`.
2. Every key in `onboard_knobs.AREA_KEYS` has exactly one `Area`; every asked tail in the
   registry has a `LINES` entry and an `EXAMPLES` entry.
3. **Prose has a floor.** `summary`, `on_costs` and `off_costs` are each at least 80
   characters, each distinct from the other two within the same area, and each ends in a
   full stop — the repo already has this idiom as `specs.MIN_PROFILE_CHARS`. "Non-empty"
   passes on `"x"` and is this section's weakest assertion without the floor.
4. **A preset only moves knobs its own area owns.** For every area, every posture and every
   tail in that posture, `onboard_knobs.area_of` of that tail spelled at the area's scope
   returns this area, and `asked` is true for it. Without this, a `validation` posture that
   quietly sets `os.neo.enabled` passes — this is this section's half of the registry seam.
5. Every preset survives being written into a document and parsed by
   `catalog.parse_catalog`, with the resolved value read back equal.
6. `onboard_explain` answers for every area key and every asked tail, with `apply` and
   `safety` equal to `ops.apply_class` / `ops.safety_key` for the same path; an unknown
   target is refused with a message listing the areas; `explain` with no project still
   answers.
7. **The command it hands the user is a command.** The `jarvis config set …` string in the
   path-target payload parses through `cli.build_parser()` once the leading `jarvis` is
   stripped, and for a project-scoped tail it carries the project positional. It is the one
   string a user is most likely to copy.
8. **Payload shapes are pinned**: the exact key set of the area-target payload and of the
   path-target payload, so a half-filled dict fails.
9. **Numbers come from defaults, mechanically as far as is honest**: for each asked tail
   whose shipped default is numeric, if its `EXAMPLES` string contains any number, at least
   one of those numbers equals that default. The spec-sourced half of the rule — that a
   dollar figure in prose is traceable to a spec — is review, not test, and the brief says
   so rather than faking a test for it.
10. **The wiring wording cannot fork.** The wiring area's text contains a substring read at
    test time from `jarvis.ui.app.WIRING_GROUPS`, which this section quotes and never edits.

`src/jarvis/onboard_knobs.py` is read-only here: `AREA_KEYS` is the input, not something to
extend when a key is awkward. No new `docs/os/` pages.

## 5. The greeting_agent session, and the operator doc

**The persona asset: `src/jarvis/assets/greeting-agent/greeting_agent.md`** —
frontmatter (`name: greeting_agent`, `description`, `tools`) plus prose, the shape every
other persona in `assets/` has. **The asset directory is flat**: no asset dir in this repo
carries a dot-directory, and a `.claude/` inside `src/jarvis/assets/` raises a packaging
question the shipped idiom avoids. It is materialised by the shipped, already-tested
`bootstrap._rebuild(ASSETS / "greeting-agent", paths.onboarding_dir() / "agent", "agents")`
— the same copytree `install_agent_assets` uses to give a worker its crew — which produces
`paths.onboarding_dir()/agent/.claude/agents/greeting_agent.md`, and the session is launched
with `--add-dir <that root> --agent greeting_agent`, which is how the OS gives a LEAD
session its persona (§0). **Verify that combination in an interactive (non-`-p`) session
first**: the repo only exercises `--agent` for dispatched and headless sessions. If it does
not take, fall back to `--append-system-prompt-file` with the same body — the asset and its
content do not change either way, only the flags do, and say in the finish summary which
one shipped.

What the prose must establish, because these are the ways this session goes wrong:

- It is talking to a human deciding how an autonomous fleet will behave on their code. It
  asks ONE area at a time in registry order, offers the postures it was given, and never
  invents a question or a setting.
- **It never edits JSON and never writes the catalog.** Every read and write goes through
  `jarvis onboard questions`, `jarvis onboard explain`, `jarvis onboard apply`.
- **It never explains a knob from its own knowledge.** When asked "what happens if I turn
  this on?" it runs `jarvis onboard explain <area|path>` and answers from that output —
  summary, on/off costs, the worked example, the apply class, whether it is a safety key,
  the grounding excerpt. If `explain` has nothing, it says so rather than guessing.
- It offers "accept the defaults for this section" every time, and "accept the defaults for
  everything else" once the must-have areas are answered.
- It asks the user, in one question, **why** they are choosing what they chose, and records
  it as the draft's `reason` — because most of these are safety keys and the write is
  refused without one (§0.3).
- When the catalog already holds projects it surfaces the fleet-wide warning for any `os.`-
  scoped area before touching it (§2).
- Before applying it shows `jarvis onboard apply --from … --dry-run` and waits for the user
  to say go. Afterwards it reports `restart_needed` and prints the `jarvis start` command.
- It opens no branch, no PR and no work order, and does no project work. If the user asks
  for project work mid-interview, it says that is a work order and finishes first.

**`ops.onboard_session(path, name=None)`** returns everything a launch needs and runs
nothing: the resolved project path and name, whether the project is already in the catalog,
the draft it created (`interview.new_draft`, pre-filled on a re-run per §2), the
materialised persona root, the opening user prompt (the draft path, the areas still
unanswered, and the four commands), and the argv. **The argv is a pure function**
(`launch_argv(...) -> list[str]`) so it is unit-testable with no model and no process:
`[claude_cli.claude_bin(), "--add-dir", <persona root>, "--add-dir", <project path>,
"--agent", "greeting_agent", <opening prompt>]`.

**The cwd is a decision, not a detail.** `bootstrap.ensure_trust` writes workspace trust
into `~/.claude.json`, and it runs during `bootstrap_project` — i.e. AFTER the interview.
An interactive `claude` whose cwd is the not-yet-adopted project would hit Claude Code's
trust prompt before the interview starts. So the session runs with **cwd =
`paths.jarvis_home()`** and reaches the target through `--add-dir`. `cli.cmd_onboard_start` calls
`ops.onboard_session` and then `os.execvp` on the argv — a three-line wrapper around the
pure function, and **nothing in the suite execs `claude`**. `--print-prompt` prints the
persona root and the opening prompt instead of exec'ing: the escape hatch when `claude` is
not on `PATH` or the user wants to paste the interview into a session already open.

**The operator doc.** `docs/os/operator/onboarding.md`, linked from `docs/os/index.md`
under "Operating the OS" beside `orders.md` / `reviews.md` / `spend.md` / `records.md`:
what onboarding is, the four commands, how an interview runs, what a posture is, that
accepting defaults writes nothing, that the whole interview lands as ONE config version
visible in `jarvis config history`, that a new project needs `jarvis start` before the
daemon sees it (§3), and how to re-run the interview to revise a project. It names
`jarvis adopt` as the non-interactive path that remains, and `cli.cmd_adopt`'s help string
gains one clause pointing at `jarvis onboard`.

**The crib sheet.** `CLAUDE.md` gains **one line** in the command block beside
`jarvis adopt <path>`, in the register of the lines around it. One line, not four:
`tests/test_persona.py` puts structural guards on that file and
`tests/test_worker_start_budget.py:85` caps it at 5000 tokens.

**Tests — `tests/test_onboard_session.py`**, plus two existing files.

1. **Every backticked `jarvis …` command in the persona body PARSES.** Copy
   `tests/test_worker_brief.py:116` (`test_every_core_command_string_parses`) including its
   `re.findall(r"`(jarvis [^`\n]+)`", text)` and its `<...>` placeholder substitution, and
   run `cli.build_parser().parse_args` on each, failing with the offending command. This is
   the assertion that keeps the persona honest: a substring check survives a sibling
   renaming `--from` to `--draft`, and `parse_args` does not. It is the house idiom, not an
   invention.
2. **The reverse guard on the surface.** The `onboard` subparser's choices are exactly
   `{start, questions, explain, apply}` — idiom at `tests/test_rules_cli.py:221`,
   `parser._subparsers._group_actions[0].choices["onboard"]` — so a sibling ADDING a fifth
   command also goes red until the persona and §0.2 are updated. The same test asserts
   `cli.build_parser().parse_args(["onboard", "start", "/tmp/x"])` resolves, which is what
   keeps the entry point a named subcommand rather than a bare path.
3. `onboard_session` on a fresh git project returns a draft with every area unanswered and
   an argv carrying `--agent greeting_agent` and an `--add-dir` for the persona root; on an
   already-adopted project the draft is pre-filled from the live resolved config and the
   payload says so; a path that is not a git repository is refused before anything is
   written.
4. **Asset identity.** The materialised `<root>/.claude/agents/greeting_agent.md` is
   byte-identical to the packaged asset, and `seats.parse_definition` of it gives
   `name == "greeting_agent"` with a non-empty body. Import **`seats`**, whose
   `parse_definition` this is; `panel` only re-exports it, and importing `panel` here would
   read as a panel dependency this section does not have. `parse_definition` does NOT check
   `name` against the filename, so the test asserts the name itself.
5. **cwd.** The reported cwd equals `paths.jarvis_home()` and is not inside the project
   path; `--print-prompt` created the persona root and the draft and returned no argv.
6. **The crib line, in `tests/test_persona.py`** (its own idiom,
   `test_crib_sheet_documents_every_improvement_order_verb`): `jarvis onboard start <path>` is in
   `CLAUDE.md`, and at most ONE line of it starts with `jarvis onboard`. Nothing else in
   this list asserts `CLAUDE.md` changed at all, so without it a worker can finish believing
   it is done with the crib entry missing. `tests/test_worker_start_budget.py:85`'s
   5000-token cap stays green.
7. **The doc is reachable**: `docs/os/operator/onboarding.md` exists AND `docs/os/index.md`
   contains `operator/onboarding.md`. An unlinked page is invisible.
8. The `adopt` subparser's help string contains `jarvis onboard`.

`project_store.WO_KINDS`, `dispatch.py`, `hooks.py` and `daemon.py` take a zero diff.
Nothing in the suite execs `claude`.

**One manual check, reported in the finish summary**, standing in for the eval this feature
deliberately does not ship: run `jarvis onboard start <scratch git repo>` by hand, confirm
`--add-dir <root> --agent greeting_agent` takes in an interactive (non-`-p`) session,
answer two areas, confirm `apply --from … --dry-run` prints the diff, and state whether
`--agent` or the `--append-system-prompt-file` fallback shipped.

## Agent profile

You are a Jarvis OS engineer implementing one section of the greeting-agent onboarding
interview. You work in your own git worktree on the OS's own repository, you open one pull
request, and you touch only the files your section names.

**What you must know about this codebase before you write anything.**

The CLI is the OS: every read and write of state goes through a `jarvis …` command, and
`ops.py` is the single implementation that both `cli.py` and the FastAPI dashboard call.
Never poke SQLite, session files or the catalog JSON from anywhere but `ops`. Imports run
strictly downward — leaves (`paths`, `db`, `catalog`, `claude_cli`, `sections`,
`spec_index`) then stores then adapters then `dispatch`/`ops` then `daemon`/`cli`/`ui` —
and `cli.py` imports every `jarvis` module lazily inside the handler that needs it. A new
module needing something from `ops` does that import inside the function, as the rest of
the codebase does; do not create an upward edge at module level.

Configuration is a content-addressed version ledger, not a file you edit:
`os_config_versions` in `os.db` is the record, the catalog file is a materialised view, and
`ops._commit_document` is the only writer — validate, write the file atomically, record the
row, in that order. `config_version.resolve()` flattens a parsed catalog to
`dotted path -> value` and `config_version.diff()` compares two such maps; use them rather
than walking the document yourself. `catalog.parse_catalog` is the type system for a
configuration value — never write a second one. Head is read through
`CentralStore.head_config_version()`, never off `config_versions()[0]`.

**The traps, each of which has cost a work order before.**

- `tests/` is routinely run by a worker that inherits `JARVIS_WO_ID`, and
  `ops._refuse_worker_write` reads exactly that variable, so a test that writes
  configuration needs the autouse fixture that deletes it — copy the one in
  `tests/test_config_console.py`. Do not scrub the variable globally; something else needs
  it.
- Do NOT run the full test suite. Run the files covering what you changed
  (`uv run pytest tests/<file> -q`). CI runs everything on three Python versions, which is
  strictly more than you can run locally.
- `projects` is a LIST in the catalog document, addressed by each entry's `name`.
  `ops._document_slot` is the only place that knows how a dotted path lands in the
  document; go through it.
- `ops._key_path` refuses an unknown project, which is why onboarding needs its own write
  path and cannot be built out of `jarvis config set` calls.
- Most of what this interview configures is a `catalog.SAFETY_KEYS` match, so
  `ops._require_reason` refuses the write without a reason. The reason comes from the
  user, in their words.
- `Daemon.reload_catalog` refuses to reload at all when the set of project names changed,
  and freezes every other setting change in the file until `jarvis start`. Never work
  around that by hot-adding a project.
- Gates govern dispatched workers only: `hooks.gate_decision` returns `None` when
  `JARVIS_WO_ID` is unset, and `hooks.preflight_decision` then auto-allows any `jarvis …`
  chain. An interactive greeting session is not gated; a worker is. Do not "fix" that.
- No test may exec the real `claude` binary. `jarvis/testing.py` ships a fake; assert on
  argv and on files written, never on a launched process.

**Conventions.** Match the surrounding code's comment density: a comment states the WHY the
code cannot show, in one line, and CI enforces that. House style is `caveman` at level
`full` and `ponytail` at level `full` for everything you write — summaries, PR body, commit
messages, comments. The smallest change that fully solves your section, including the
callers and tests your change breaks. New non-trivial logic leaves one small test.

**What you must never do.** Do not implement a sibling's section, even if it is two lines
and even if yours would be easier with it; say so in your finish summary instead. Do not
add a configuration knob, a CLI command or a flag the spec does not name. Do not touch
`WO_KINDS`, `dispatch.py`, the daemon, `hooks.py`, the `/config` dashboard tab or the
launcher: a greeting session is not a work order, and launcher-contract negotiation is
explicitly out of scope. Do not revive `src/jarvis/onboarding.py` or
`src/jarvis/launcher.py` from the unmerged branch `worktree-wo-036e8be2`. Do not write a
configuration default into the catalog document because the user accepted it — accepting a
default must leave the document silent. Do not add a new invariant. And do not invent the
impact of a setting: if you cannot ground it in a spec section, in the config code or in a
resolved default, say that you could not.
