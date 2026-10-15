# Make the symbol index the cheap path for workers, and measure whether they take it

Feature order `fo-b9a3fb06`. Spec written by the planner, 2026-10-02. Filed from improvement
order `io-edacb3ea`. The one checkout to touch is the worktree
`/home/gonzalo/workspace/agentic_os/.claude/worktrees/wo-1abd3886`; every path below is
absolute or relative to it.

Sections 3, 4, 5, 6 and 7 are the work, one work order each. Sections 1 and 2 are the
background every child needs and belong to nobody. Section 8 is the boundary.

---

## 1. What is broken, and why reading harder cannot fix it

Workers do not use Serena's symbol tools. The prose fix for that already shipped and it did
not work. `worker_brief.navigation_core`
(`/home/gonzalo/workspace/agentic_os/.claude/worktrees/wo-1abd3886/src/jarvis/worker_brief.py:572`)
already opens with the line `# Finding code: your symbol tools are DEFERRED, not absent`
(`:592`), already hands out `NAV_SELECT_LINE` (`:566`) carrying the exact `ToolSearch` select
call for all four tools, and the measured result is approximately zero symbol calls per
order. Louder prose is dead. This feature is mechanical or it is nothing.

Two separate mechanical traps stand between a worker and a symbol call, and they are
independent.

**Presence.** The Serena read tools are DEFERRED behind `ToolSearch` in a dispatched session.
So `worker_brief.navigation_section` (`src/jarvis/worker_brief.py:606`) asks the worker to
spend a `ToolSearch` call before it can navigate, and the clause "if its symbol tools appear
in your tool list" reads false. Measured live on this machine with `claude -p --settings` and
`--output-format stream-json`, reading the `init` event: **92 tools and no `ToolSearch` with
`ENABLE_TOOL_SEARCH=false` in the settings file's `env` block, versus 45 tools plus
`ToolSearch` by default.** That is approximately 47 extra tool schemas in the static prefix,
whose token cost is UNMEASURED. With the flag off, a probe called
`mcp__plugin_serena_serena__find_symbol` directly with no `ToolSearch` call at all, so
presence works.

**Activation.** That direct call then FAILED, verbatim:

```
Error executing tool find_symbol: No active project. Ask the user to provide the project path or to select a project from this list of known projects: ['jarvis-os', 'jarvis-os', ...]
```

It failed despite a COMMITTED `.serena/project.yml` in the worktree. `~/.serena/serena_config.yml`
holds 102 entries, one per Jarvis worktree, ALL named `jarvis-os`. An `activate_project` on
this worktree answered `Created and activated a NEW project`, meaning the path was not
registered at all. So present is not usable, and activation is a second trap with its own
unknown mechanism. The server itself is plugin-launched from
`/home/gonzalo/.claude/plugins/marketplaces/claude-plugins-official/external_plugins/serena/.mcp.json`
as `uvx --from git+https://github.com/oraios/serena serena start-mcp-server`, with no
`--project`.

Done, for the whole feature: a fresh worker's first navigation call is a symbol call.

## 2. What already exists, the standing rules, and the dependency edges

**The template every switch in this feature copies.** The bash-first steer already shipped as
catalog key `worker.bash_first` (default `off`, valid values at `src/jarvis/catalog.py:82`)
feeding `dispatch.bash_first_env`. It is NOT this feature's work and nothing about it changes.
It is the shape, quoted verbatim from `src/jarvis/dispatch.py:56-71`:

```python
BASH_FIRST_ENV = "CLAUDE_CODE_THRIFTY_SONIC"
BASH_FIRST_VARIANT_ENV = "CLAUDE_CODE_COZY_TEAPOT"

def bash_first_env(bash_first: str) -> dict[str, str]:
    if bash_first == "cli":
        return {}
    if bash_first == "off":
        return {BASH_FIRST_ENV: "false"}
    return {BASH_FIRST_ENV: "true", BASH_FIRST_VARIANT_ENV: bash_first}
```

`_write_worker_settings(project, wo)` (`src/jarvis/dispatch.py:80`) splats the result into the
`env` dict at `src/jarvis/dispatch.py:233`, beside `JARVIS_SERENA` (`:188`), `JARVIS_GATES`,
`MCP_TOOL_TIMEOUT` (`:226`) and `**claude_cli.PROMPT_CACHE_5M_ENV`, then writes
`<project.path>/.jarvis/worker-settings/<wo-id>.json`.

**The standing rules. Quoted once here; every section below is bound to them and cites them
rather than restating them.**

> **Catalog-key discipline** (`kn-f9a483d6`, `kn-1cec46b5`): a switch is a catalog key under
> `worker.*`, resolvable fleet-wide and per project, NEVER a module constant.

> **The masking rule** (`kn-7f5f2d0d`): a PreToolUse hook that judges shell structure on RAW
> text refuses prose. Quoted spans must be masked asymmetrically by quote kind.

> **The `preflight_decision` ordering invariant**: an arm's POSITION in the Bash chain
> (`src/jarvis/hooks.py:1902`) is load-bearing. The `is_jarvis_command_chain` auto-allow at
> `src/jarvis/hooks.py:1980` returns an `_allow`, so a refusal placed AFTER it is unreachable
> in production however green its unit test is.

> **Absent is not zero** (issue #227's rule): a reading that could not be taken renders as
> `unclassified` or *not recorded*, never as `0`.

**Dependency edges.** Four are real:

1. Section 3 to section 6. The hook imports the leaf; shipping the hook with its own copy of
   the predicate is the duplication this feature exists to kill.
2. Section 4 to section 7.
3. Section 5 to section 7.
4. Section 6 to section 7.

Two edges are FALSE and no child re-argues them. **Section 3 to section 4 is false**: the
files are disjoint — the counter is `inspection.py`, `cli.py` and `_debug_anatomy.html`,
presence is `catalog.py`, `dispatch.py` and `worker_brief.py` — and the "baseline first"
argument does not survive contact with `--depends-on`, which clears on COMPLETE, not after a
week of fleet traffic. Sections 3, 4 and 5 dispatch together; 3 lands first anyway because it
is smallest. **Section 4 to section 6 is false**: it would be real only if the hook shipped
ON, and the hook ships off.

**Standing constraints every child inherits.** Work only in the assigned worktree and never
write anywhere under `~/workspace/production`. The CLI is the OS: never poke SQLite, session
files or project state directly. Privileged actions (`pr_merge`, `release`,
`service_restart`) are GATED — request with
`jarvis gate request <wo-id> "<exact command>" --why … --evidence …` and END the turn; never
retry a blocked command verbatim, and contest a false positive with `jarvis gate contest`
rather than rewording it. Never bare `git stash` or `git stash pop`, because the stack is
shared across more than 100 worktrees: use a WIP commit, or
`git stash push -u -m "<unique-tag>"` plus `apply <sha>`. Do NOT run the full suite locally —
it takes 21 to 22 minutes and blows the 5-minute prompt cache; run the targeted command named
in your own section, open the PR, and cite CI (3.11, 3.12, 3.13 plus evals). Run
`uv sync --extra dev` first in a fresh worktree. House style governs every byte, including PR
bodies and code comments: one line of comment citing the spec, never the explanation. Doubt
goes to Neo first with `jarvis wo ask <wo-id> "…"` followed by ending the turn; a call made
with NO doubt is recorded with `jarvis wo assume <wo-id> "…"`; deferred work goes to
`jarvis backlog add jarvis_os "…"`. You are a LEAD: code and tests go to
`jarvis-implementer` (test first, failing first), spec text to `jarvis-spec-writer`, and your
own `Edit`/`Write` in the worktree is hook-refused.

## 3. The counter: classify every call as a symbol call or a source-navigation Bash call

Child 1, key `classify+count`. Deliver a per-order reading of symbol calls versus
source-navigation Bash calls on `jarvis inspect`, on `jarvis inspect --json`, and on the debug
page.

**The leaf.** A new stdlib-only module `src/jarvis/navigation.py` exporting `NAV_COMMANDS`,
`SOURCE_SUFFIXES`, `navigates_source(command: str, suffixes: tuple[str, ...]) -> bool` and
`is_symbol_call(tool_name: str) -> bool`. MOVE `hooks._mask_shell_text`
(`src/jarvis/hooks.py:480`) and `hooks._statements` (`src/jarvis/hooks.py:590`) into it and
re-export from `hooks` BY IDENTITY. A second masker is the defect the masking rule of section
2 exists to prevent. The leaf imports nothing from `jarvis`: `hooks.py` imports it on every
Bash PreToolUse, so an import of `catalog` there is a per-command cost.

**Where to classify.** At the `ToolSpan(...)` construction in `src/jarvis/inspection.py:1191`,
reading `block.get("input")` — the same raw read that `backgrounded=` already does at
`src/jarvis/inspection.py:1199`. NOT from `detail`: `_detail_of`
(`src/jarvis/inspection.py:1052`) prefers `description` over `command`, so most Bash spans'
`detail` is the model's prose. NOT from `params` either: `ParamCaps`
(`src/jarvis/inspection.py:403`) truncates at 500, 2,000 and 20,000 and drops keys.

**The fields.** A new `ToolSpan.navigates_source: bool`, additive exactly like `backgrounded`
(`src/jarvis/inspection.py:493`). A new `Anatomy.nav_profile()` beside `tool_profile()`
(`src/jarvis/inspection.py:914`). A new `"nav"` key in `Anatomy.as_dict()`
(`src/jarvis/inspection.py:1010`) carrying `symbol_calls`, `source_nav_calls` and
`unclassified` — those three names are the contract that the children of sections 4 and 5
read.

**Render** in `cli._print_anatomy` (`src/jarvis/cli.py:2196`, beside the tool-profile block it
prints) and in `src/jarvis/ui/templates/_debug_anatomy.html` beside the `Tool profile`
heading at `:114`, with the same literals in both.

**The seal.** Raise `autopsy.PAYLOAD_VERSION` (`src/jarvis/autopsy.py:63`) to 2, and
`_upgrade_seal` (`src/jarvis/autopsy.py:457`) re-derives the nav counts from the stored
payload's `turns[].spans[].params["command"]`. That re-derivation is LOSSY, because
`params_dropped` keys cannot be re-derived, so an upgraded seal reports `unclassified` under
the absent-is-not-zero rule of section 2.

**A finding that changes the tests.** The committed fixture
`tests/data/transcripts/-wo-5a6b2d6d/ec8236c7-b418-4f09-80f0-1edea61f099f.jsonl` has NO
`"command"` key anywhere — verified, zero occurrences;
`scripts/redact_transcript.py` (docstring at `:10-14`) kept only `description`. The
`real_session` fixture's 55 Bash calls therefore CANNOT be classified and must report
`unclassified == 55` and `source_nav_calls == 0`. That is the best anti-vacuity test in the
feature, and it shares its reason with the lossy seal upgrade.

**Tests, new `tests/test_navigation.py`, stdlib only and no fixtures.**

- `test_a_grep_at_a_py_path_navigates_source`.
- `test_a_recursive_sweep_of_the_tree_navigates_source`: `grep -rn "def foo" .` and
  `rg foo src` are both True.
- `test_bookkeeping_reads_do_not_navigate_source`: `cat tool-log.jsonl`, `jq . payload.json`,
  `uv run pytest tests/test_x.py` (the trap — it ends in `.py` but the command word is not in
  `NAV_COMMANDS`), `git log --oneline`, `jarvis wo show wo-1`.
- `test_sed_counts_only_with_dash_n`: `sed -n '1,40p' a.py` True, `sed -i s/x/y/ a.py` False.
- `test_a_py_path_inside_a_quoted_string_does_not_count`: `git commit -m "fix pricing.py"` is
  False, which proves the masker is applied and not merely re-exported.
- `test_the_suffix_set_is_an_argument_not_a_global`:
  `navigates_source("grep -rn x notes.md", (".py",))` False and the same call with `a.py`
  True. Section 6's child depends on this call shape.
- `test_is_symbol_call_accepts_both_serena_prefixes`: both `mcp__serena__find_symbol` and
  `mcp__plugin_serena_serena__find_symbol`, plus `LSP`.
- `test_serena_text_search_is_not_a_symbol_call`: `search_for_pattern` is False. Without it
  the feature can be won by swapping one text search for another.
- `test_hooks_re_exports_the_moved_helpers_rather_than_copying_them`: identity with `is`, not
  equality, because a copied body passes equality and then drifts.
- `test_the_leaf_imports_nothing_from_jarvis`.
- `test_a_command_too_long_for_params_is_still_classified`.

**Tests extending `tests/test_inspection.py`**, reusing `write_transcript`, `prompt_row`,
`tool_rows` and `rendered` (`tests/test_inspection.py:2118`).

- `test_a_bash_span_is_classified_from_the_raw_input_not_the_detail`: with input
  `{"command": "grep -rn total_for src/pricing.py", "description": "look for the total"}`,
  assert `navigates_source is True` AND `detail` unchanged; then with the redacted shape,
  description only, assert False.
- `test_the_nav_profile_counts_symbol_calls_and_source_greps`: 2 `find_symbol` calls, 3 source
  greps, 1 `search_for_pattern` and 1 `cat notes.md` give 2, 3 and 0.
- `test_a_redacted_transcript_reports_unclassified_not_zero`: the `real_session` finding above.
- `test_the_tool_profile_is_unchanged_by_the_nav_block`: re-assert what
  `tests/test_inspection.py:121` pins — `rows["Bash"]["calls"] == 55`, `45.6`, `0.8`, total
  `81`.
- `test_the_nav_counts_are_printed_in_the_tools_block`.

`tests/test_ui_debug.py` gains `test_the_anatomy_page_shows_the_navigation_counts`.

`tests/test_autopsy.py`, beside the upgrade block at `:1096`, gains
`test_an_upgraded_seal_re_derives_nav_from_the_stored_command` and
`test_an_upgraded_seal_reports_unclassified_rather_than_zero`. Those two are a PAIR and both
must be present: the first alone permits reporting zero, the second alone permits reporting
everything as unclassified. `test_nothing_to_upgrade_leaves_the_seal_alone`
(`tests/test_autopsy.py:1119`) must stay green with `PAYLOAD_VERSION = 2`.

**Command.**

```
uv run pytest tests/test_navigation.py tests/test_inspection.py tests/test_autopsy.py tests/test_autopsy_read.py tests/test_ui_debug.py -q
```

**Must not change.** Every number in `tests/test_inspection.py` and `tests/test_autopsy.py`
that exists today. `ToolSpan.detail` stays byte-identical. `tool_profile()` keeps its keys and
its ordering: `nav_profile()` is a sibling, not a replacement. Do NOT edit
`scripts/redact_transcript.py` to start keeping commands. Do not touch the catalog key,
`dispatch.py` or `worker_brief.py` — section 4 owns presence and its key. Do not add the hook:
section 6 owns `py_nav_decision` and its position in `preflight_decision`.

## 4. Presence: the Serena read tools in the tool list with full schemas, and no `ToolSearch`

Child 2, key `present`. Deliver a dispatched worker whose tool list contains the Serena read
tools with full schemas and no `ToolSearch`.

**The key.** A three-state catalog key under `worker.*` obeying the catalog-key discipline of
section 2: `off` writes `ENABLE_TOOL_SEARCH=false`, `on` writes `"true"`, and `cli` writes
neither key and leaves the vendor default alone. NOT a bool —
`docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md` section 4 already argues
the three-state shape in its rejected alternatives, so do not re-argue it.

**The sites, exactly the `bash_first` shape of section 2.** `src/jarvis/catalog.py:426` for the
`WorkerDefaults` field, `src/jarvis/catalog.py:1304` for `OsConfig.default_*`,
`src/jarvis/catalog.py:2077` to read `os.defaults.*`, and `src/jarvis/catalog.py:2119` plus
`src/jarvis/catalog.py:2156-2177` to validate, naming the key and all three valid values. A
sibling of `dispatch.bash_first_env()` near `src/jarvis/dispatch.py:60`. The splat in
`_write_worker_settings`. One `("*.<key>", "next-dispatch")` row at `src/jarvis/ops.py:10793`,
after which `jarvis config set` works with no further code.

**The brief flip is THIS work order, not a later one.** `worker_brief.navigation_core` becomes
a falsehood the instant the env flips, and `tests/test_worker_brief.py:307` plus the assertion
at `:311` pin the wording. Two PRs here means a window in which every worker in the fleet is
told to make a `ToolSearch` call it does not need. Keep `NAV_SELECT_LINE` and the DEFERRED
wording REACHABLE for the `on` and `cli` states: rewrite those tests at the tool-search-on
state, and do not delete or `xfail` them. `tests/test_worker_crew.py:120` asserts the crew
block repeats the navigation wording word for word, so a reword breaks it.

**Tests, new `tests/test_tool_search.py`**, line for line the shape of
`tests/test_bash_first.py` (74 lines — read it first), using its `_env(project, …)` helper
from `tests/test_bash_first.py:27`:

- `test_off_lists_every_tool_and_writes_false`.
- `test_on_restores_the_vendor_default_explicitly`.
- `test_cli_writes_the_key_at_all`, asserting the key is ABSENT.
- `test_the_project_default_is_the_shipped_state`, naming the literal.
- `test_every_env_value_is_a_string`. Claude Code's `env` is `Record<string,string>` and an
  integer can make the CLI reject the whole settings file.

`tests/test_catalog.py`, beside `test_bash_first_defaults_off_and_overrides_per_project` at
`:116`, gains `test_tool_search_defaults_and_overrides_per_project` and
`test_an_invalid_tool_search_names_the_key_and_the_valid_values`, where the `CatalogError`
message contains the key and all three values, because that string IS what `jarvis config set`
shows. Add a row to the parametrised bad-value block at `tests/test_catalog.py:160`.

`tests/test_config_console.py:301` gains
`("projects.p.worker.tool_search", "next-dispatch")`.

`tests/test_worker_brief.py` gains
`test_the_navigation_block_does_not_call_the_tools_deferred_when_tool_search_is_off` and
`test_the_select_line_survives_for_the_tool_search_on_states`. The second asserts that
`NAV_SELECT_LINE` — the literal pinned at `tests/test_worker_brief.py:285` — is still present
when tool search is on, and that `navigation_core` still names all four of `find_symbol`,
`find_referencing_symbols`, `get_symbols_overview` and `activate_project` in every state.
`tests/test_worker_brief.py:320` already asserts that last part; keep it green.

**Command.**

```
uv run pytest tests/test_tool_search.py tests/test_bash_first.py tests/test_catalog.py tests/test_worker_brief.py tests/test_config_console.py tests/test_worker_crew.py evals/test_prefix_drift.py -q
```

**The PR body carries the measurement, and the wording has to be unfakeable.** From one real
probe turn per state: the session id, the tool count from that turn's `init` event, and the
`cache_creation_input_tokens` of its first assistant message, for BOTH the flag-on and the
flag-off state. The words "approximately", "estimated" and "roughly" must not appear in that
paragraph. The anchor to beat is section 1's measurement: 92 tools and no `ToolSearch` off, 45
tools plus `ToolSearch` by default. Also check explicitly whether the env in the settings file
survives a `--resume` turn as well as an opening one: a worker whose tool list changes
mid-conversation is a prefix break.

**Must not change.** `tests/test_bash_first.py`, not one line. `evals/test_prefix_drift.py:246`
and `:257` pass unedited, and `HEAD_SHARE_FLOOR` (`evals/test_prefix_drift.py:58`) is not
lowered to make them pass. `worker.bash_first` behaviour is untouched. This child does NOT
flip the fleet default — section 7 owns the flip, and it owns the eval that decides whether
the flip is earned. Activation is section 5's: do not write a Serena project path or name into
the settings file here.

## 5. Activation: a first `find_symbol` that returns symbols rather than `No active project`

Child 3, key `activate`. Deliver, in a worker worktree with a committed `.serena/project.yml`,
a fresh worker whose first `mcp__plugin_serena_serena__find_symbol` returns symbols rather
than the `No active project` error quoted in section 1, with no sentence anywhere in the brief
telling the worker to call `activate_project` first.

This is its own child because it is the only piece whose MECHANISM is unknown. Folding it into
section 4 would make that section unsizeable: section 4 is a mechanical catalog-key edit, this
is research that might end in "no clean mechanism exists".

**Probes, cheapest first, all minutes of shell.**

1. `uvx --from git+https://github.com/oraios/serena serena start-mcp-server --help`. Is there
   an env var, or a `--project .` that resolves per-cwd? If an env var exists, this child
   collapses into one more key in the `_write_worker_settings` env dict of section 2.
2. Why cwd auto-activation fails despite the committed `.serena/project.yml`. Section 1
   measured that `activate_project` on this worktree answered
   `Created and activated a NEW project`, so the path was not registered, and the 102 entries
   all named `jarvis-os` are other worktrees. Serena likely matches the registry by NAME and
   the name collides. If so the fix may be a per-worktree `project_name` — but
   `.serena/project.yml` is COMMITTED and shared by every worktree, so it cannot carry a
   unique name, and dispatch would have to write an untracked override, which fights "one
   committed map".
3. Fallback: a `SessionStart` hook `additionalContext` line carrying the literal
   `activate_project` call with the worktree path already filled in. The `SessionStart` branch
   is `src/jarvis/hooks.py:2815` and `concision.house_style()` at `src/jarvis/hooks.py:2854`
   is the existing `additionalContext` precedent. This is still prose, but it is prose at the
   moment of failure with no path left for the model to guess — a different thing from the
   brief's standing advice, and the only mechanism left that does not touch the vendor.

**Probe 3 is a FLOOR, not an option.** This child ships a working mechanism. If probes 1 and 2
both fail, probe 3 ships. A PR whose outcome is a findings document and no code change is a
rejection.

**The structural assertion that holds in EVERY branch**: the string Jarvis writes names this
work order's OWN worktree path, not the main checkout. That is the specific thing the
102-entry name collision would otherwise break silently. Tests, by branch:

- `test_the_worker_settings_name_the_project_root_for_serena`: the path appears in the
  settings file exactly once and is the work order's worktree.
- `test_the_serena_project_name_is_unique_per_worktree`: two work orders in the same repo get
  different names. A fix that still produces two identical names has not fixed it.
- `test_session_start_injects_the_activate_call_with_the_worktree_path`: the returned
  `additionalContext` contains the literal `payload["cwd"]` worktree path and the literal tool
  name `activate_project`. Plus `test_no_activate_context_outside_a_worker_session`.

**The half that is NOT unit-testable** is whether the vendor actually activates. CI cannot
answer it and a mocked Serena asserts only Jarvis's own belief. So the proof is a hand probe
and it is a REQUIRED artefact: run one real `claude -p` turn in a scratch worktree with the
shipped settings, whose prompt is a single `find_symbol` call, and paste the tool result
VERBATIM into the PR body — the symbol list on success, the error string on failure. A PR with
no pasted tool result has not proved this child. Commit the probe prompt as a script under
`scripts/` so the next session can repeat it. The Serena MCP server is `pending` for the first
seconds of a session: sleep approximately 25 seconds before the first tool call or the probe
reads ABSENT falsely.

**Explicitly NOT by adding a second Serena MCP server**, and here is why in full, because it
is the obvious wrong answer.

1. There is nowhere to put it. `_write_worker_settings` writes a Claude Code SETTINGS file,
   which has no `mcpServers` key, and `claude_cli.turn_args`
   (`src/jarvis/claude_cli.py:849`) and `_briefing_args` (`src/jarvis/claude_cli.py:374`) pass
   no `--mcp-config` on the worker path at all. Only the toolless headless path does
   (`src/jarvis/claude_cli.py:1884`, with `--strict-mcp-config` and an empty config). So it
   means a new flag threaded through `_briefing_args`, `turn_args`, `spawn_turn` and
   `worker_session.briefing_for`: bigger than the whole rest of the feature.
2. A second server needs a second NAME, and the name becomes the tool prefix
   (`wiring.tool_prefix`, `src/jarvis/wiring.py:54`). A `jarvis-serena` server yields
   `mcp__jarvis_serena__find_symbol`, which is in NONE of the four places the OS names Serena:
   `dispatch.SERENA_TOOL_PREFIXES` (`src/jarvis/dispatch.py:49`, so `serena_allow_rules()`
   does not permit it and the call is blocked), `worker_brief.NAV_SELECT_LINE`, the
   `search_for_pattern` runaway-guard matchers (`src/jarvis/hooks.py:1472`,
   `tests/test_runaway_tool_call.py:140`), and `hooks.investigator_write_decision`'s read-only
   allowlist (`src/jarvis/hooks.py:1236`) — so an investigator's symbol call would be refused
   as a write. Reusing the name `serena` collides with the plugin's own entry, and duplicate
   tool names are undefined behaviour nobody has probed.
3. It defeats `wiring.serena_wired` (`src/jarvis/wiring.py:92`): a project that deselected the
   Serena plugin on /config would get Serena back through Jarvis's own server. That is exactly
   the incoherence that function exists to prevent, inverted.
4. Cost: a second `uvx --from git+…` resolve plus a language-server start per turn, and a
   worker turn is a process.

**Must not change.** `dispatch.serena_allow_rules()` (`src/jarvis/dispatch.py:74`) and
`dispatch.SERENA_TOOL_PREFIXES` keep both prefixes;
`tests/test_runaway_tool_call.py:130` pins it. The three-state presence key and the
`navigation_core` wording belong to section 4 — do not add a second switch for them here. Do
not add the Bash refusal: section 6 owns `py_nav_decision`.

## 6. The hook: refuse a source-navigation Bash call at a `.py` path, with the alternative named

Child 4, key `hook`. Deliver, with its catalog key on and in a project that has
`.serena/project.yml`, a worker whose `grep -rn "def total_for" src/pricing.py` is refused
with a message naming both `find_symbol` and `activate_project`, while
`grep -rn coupon README.md` runs.

**What to build.** `py_nav_decision` in `src/jarvis/hooks.py`, modelled on
`investigator_bash_decision` (`src/jarvis/hooks.py:1261`) and `long_foreground_decision`
(`src/jarvis/hooks.py:723`). Behind its own catalog key per section 2's discipline, DEFAULT
OFF. A hook nobody has enabled cannot strand a worker, which is what keeps section 4 off this
child's dependency list.

**Position.** In the Bash chain of `preflight_decision`, immediately after
`investigator_bash_decision` and BEFORE the `is_jarvis_command_chain` auto-allow. The
ordering invariant of section 2 is why. The docstring says so, the way
`heredoc_write_decision`'s already does (`src/jarvis/hooks.py:1159`), and also says that this
arm never has an `_allow` branch.

**Gates on two things.** `env.get("JARVIS_WO_ID")`, so interactive sessions in managed
projects are untouched; and `.serena/project.yml` existing at
`find_project_root(payload["cwd"])`, because a repo with no symbol index must keep grep or the
worker cannot read code at all.

**Suffix set `(".py",)` only**, narrower than the counter's wide `SOURCE_SUFFIXES`. Same
`navigates_source` function from `jarvis.navigation`, different argument — the call shape
section 3's `test_the_suffix_set_is_an_argument_not_a_global` pins. The hook imports the leaf
and nothing else from `jarvis`, because it runs on every Bash call.

**The deny message IS the mitigation.** A refusal that does not name
`mcp__plugin_serena_serena__find_symbol` and the `activate_project` fallback is section 1's
"louder prose, zero hit rate" failure with a 403 attached.

**Tests, new `tests/test_py_nav_hook.py`, idiom from `tests/test_runaway_tool_call.py`.**

- `test_a_grep_at_a_py_path_is_refused`: `permissionDecision == "deny"`.
- `test_sed_n_and_cat_and_head_at_a_py_path_are_refused`, parametrised.
- `test_a_grep_for_a_word_in_a_markdown_file_is_allowed`: returns `None`. This is the negative
  control, the hook twin of the eval's text-search test in section 7.
- `test_a_non_py_source_file_is_not_refused`: `grep -rn x app.ts` returns `None`.
- `test_pytest_and_git_and_jarvis_are_untouched`.
- `test_the_deny_message_names_the_symbol_call_and_the_activation_fallback`.
- `test_an_interactive_session_is_untouched`, with no `JARVIS_WO_ID`.
- `test_a_project_with_no_serena_index_is_untouched`.
- `test_the_key_ships_off`, asserting the off literal Jarvis writes.
- `test_the_refusal_is_reached_before_the_jarvis_auto_allow`. The one that matters most: call
  `hooks.preflight_decision` with `cd /repo && grep -rn "def total_for" src/a.py` and assert
  `deny`. Copy
  `tests/test_runaway_tool_call.py:143 test_the_refusal_is_reached_through_the_preflight_mcp_branch`;
  do not invent an idiom.
- `test_py_nav_runs_after_the_investigator_refusal`: with `JARVIS_WO_KIND=investigator` and a
  mutating command, the investigator's own message comes back, not this one's.

**Command.**

```
uv run pytest tests/test_py_nav_hook.py tests/test_runaway_tool_call.py tests/test_navigation.py tests/test_catalog.py tests/test_config_console.py -q
```

**Must not change.** Every other arm of `preflight_decision` keeps its current relative
position: `heredoc_write_decision`, `gate_decision`, `under_review_decision`, the two PR
checks, `finish_summary_decision`, `payload_reference_decision`, `long_foreground_decision`,
`background_task_decision` and `investigator_bash_decision`. `tests/test_pipeline.py` and
`tests/test_runaway_tool_call.py` pass unedited. The two allow-side tests, `.md` and `.ts`,
are required and must not be weakened: a hook that refuses genuine text search is a
regression, not a stricter version of this one. Out of this child: any non-`.py` suffix, the
built-in `Grep` and `Glob` tools (this is Bash-only), and flipping the key on — section 7 owns
both default flips. `navigates_source` and `_mask_shell_text` live in section 3's leaf: a
predicate defect is fixed there, never copied here.

## 7. The eval that asserts the done-when, then the two default flips

Child 5, key `eval + default flip`. Deliver the feature's done-when as an asserted scenario,
then flip the two defaults.

**The harness already exists.** `evals/llm/test_navigation_judgment.py` has `is_serena()`
(`:73`), `_statements` (`:77`), `bash_navigates_code()` (`:81`), `NAV_COMMANDS` (`:68`),
`SOURCE_SUFFIXES` (`:70`), `symbol_tools()` (`:120`), a `PreToolUse` recorder, a tmp
Serena-indexed repo, and a `worker_briefing` fixture built from the real
`build_worker_prompt`. Its gap IS the done-when: every assertion is set membership, meaning
"used a symbol tool at some point", and nothing asserts ORDER.

**Import the classifier instead of keeping a copy.** Delete those five locals and import them
from `jarvis.navigation` — the same discipline as the existing
`from jarvis.dispatch import bash_first_env, build_worker_prompt, serena_allow_rules` at
`evals/llm/test_navigation_judgment.py:46`, whose docstring says why: the arm cannot drift
from what a real spawn writes. The eval calls `navigates_source(command, SOURCE_SUFFIXES)`,
the WIDE set, not section 6's `(".py",)`.

**The ordering assertion.** Add `first_navigation_call(calls)` over the ordered tool log,
returning the first entry that is either a symbol call or a source-navigating Bash call. New
`test_no_source_grep_precedes_the_first_symbol_call` asserts it is a symbol call, in both
arms. The done-when is "**no source-navigation Bash call precedes the first symbol call**",
NOT "the first tool call is a symbol call": `activate_project`, `list_memories` and
`read_memory` are bookkeeping and legitimately come first, and `search_for_pattern` is
Serena's text search, which counts as text and never as a symbol call.

**The new arm.** Add a tool-search arm to `STEER_ARMS`
(`evals/llm/test_navigation_judgment.py:217`) and `run_and_record`
(`evals/llm/test_navigation_judgment.py:230`), taking its env from section 4's new dispatch
function BY IMPORT, not by retyping the key, exactly as `run_and_record` already does for
`bash_first_env` at `:248`.

**Keep the negative control and add its hook twin.** Keep
`test_a_genuine_text_question_may_still_use_text_search`
(`evals/llm/test_navigation_judgment.py:375`) exactly as it is, and add
`test_the_hook_does_not_refuse_a_literal_word_search_in_a_markdown_file`: the same question
with section 6's hook key on, asserting the turn produced an answer and the log contains no
denied Bash call.

**A cheap non-LLM test proving the import swap**, in `tests/test_navigation.py`:
`test_the_eval_uses_the_shipped_classifier`. Import the eval module — it imports fine without
`JARVIS_EVALS_LLM`, because the marker only skips — and assert
`ev.NAV_COMMANDS is navigation.NAV_COMMANDS` and
`ev.SOURCE_SUFFIXES is navigation.SOURCE_SUFFIXES`, and that the eval file's text contains no
`def bash_navigates_code`. Identity, so a retyped copy fails.

**The flip.** `tests/test_catalog.py` gains
`test_the_fleet_defaults_ship_tool_search_off_and_the_nav_hook_on`, asserting both new default
values by literal, and `tests/test_tool_search.py::test_the_project_default_is_the_shipped_state`
moves to the flipped value in the same commit.

**Why the ordering assertion does not live in section 6's child.** Three reasons. The LLM
evals are opt-in (`JARVIS_EVALS_LLM=1`, `evals/llm/test_navigation_judgment.py:49`) and spend
real tokens, while section 6's own tests are pure payload tests that must stay in the default
suite. The assertion measures the COMBINATION of sections 4, 5 and 6, so a failure would send
the wrong worker back. And the default flip belongs with it, because that is the one place
"done" is actually decided.

**Commands.**

```
uv run pytest tests/test_navigation.py tests/test_catalog.py tests/test_tool_search.py tests/test_py_nav_hook.py tests/test_worker_brief.py evals/test_prefix_drift.py -q
```

then

```
JARVIS_EVALS_LLM=1 uv run pytest evals/llm/test_navigation_judgment.py -q
```

which spends real tokens and needs a logged-in Claude Code with Serena available. Run it once;
paste the arm-by-arm result and the model name from `JARVIS_EVALS_MODEL` into the PR body. A
flip with no eval run is a rejection.

**The new arm is ASSERTED, not recorded.** Only the `steer` arm is `xfail`, and only because
its variant is drawn per session inside a vendor binary
(`evals/llm/test_navigation_judgment.py:220`, `xfail(strict=False)`); keep its reason. If the
new arm fails, the finding is reported and the defaults are NOT flipped.

**Must not change.** `evals/test_prefix_drift.py:246` and `:257` pass unedited,
`HEAD_SHARE_FLOOR` is untouched, and `worker.bash_first` stays `off`. Out of this child:
changing what counts as a symbol call — that is section 3's leaf, and if the eval needs a
different predicate that is a defect in `src/jarvis/navigation.py` to be fixed there; widening
section 6's suffix set; and any new eval scenario beyond the two named above.

## 8. Out of scope, and why

Listed so nobody re-proposes them mid-feature.

**Louder prose.** Already shipped, measured at approximately zero symbol calls per section 1.
Explicitly forbidden.

**Removing or narrowing Bash**, refusing non-`.py` files, or refusing genuine text search.

**`worker.bash_first`.** Already shipped. This feature copies its shape and changes nothing
about it.

**A second Serena MCP server.** Section 5 gives the four concrete reasons.

**The 102 identically-named worktree entries in `~/.serena/serena_config.yml`.** A registry
leak worth its own bug report regardless of this feature; the planner files it separately.

**Teaching `scripts/redact_transcript.py` to keep commands.** It would make future transcripts
classifiable and past ones no better, and it changes what the fixtures mean.

**The token cost of the approximately 47 extra tool schemas as a REGRESSION GUARD.**
`evals/test_prefix_drift.py` asserts prefix shape and stability, not size, so nothing will
catch a growth. Section 4 measures it once in a PR body; a standing assertion is not in this
feature.

**Counting subagent spans.** They live in `SubagentAnatomy`
(`src/jarvis/inspection.py:778`), and `kn-7a2180ba` makes subagents a partition of their turn,
so summing them silently would contradict a ruling. `nav_profile()` counts the main session;
if a later feature wants subagents it adds a separate column and says so in the renderer.

---

## Agent profile

You are a Jarvis OS engineer. Your job on this feature is to make the symbol index the CHEAP
path for a dispatched worker — present in its tool list, activated, and cheaper than grep —
and to MEASURE whether workers then take it. You are building one slice; other workers build
the others, and you will never see their sessions. Prose that tells a worker to navigate
better has already shipped and measured approximately zero symbol calls, so every change you
make is mechanical: a switch, a classifier, a hook, or an assertion.

**What you must know about this codebase.**

`jarvis_os` is a Python OS whose `jarvis` CLI is the only sanctioned interface to its state:
never read or write SQLite, session files or project state directly. The modules are layered,
and the layering decides where a helper can live — a leaf that a PreToolUse hook imports on
every Bash call may import nothing from `jarvis`, because an import there is a per-command
cost. `src/jarvis/catalog.py` owns every configurable key and its validation.
`src/jarvis/dispatch.py` owns `_write_worker_settings`, which writes the Claude Code settings
file a worker is spawned with, including its `env` block.
`src/jarvis/worker_brief.py` owns the prompt text a worker reads.
`src/jarvis/hooks.py` owns `preflight_decision`, the chain of arms that allows or refuses a
tool call. `src/jarvis/inspection.py` derives an order's anatomy from its transcript, and
`src/jarvis/autopsy.py` persists it. `src/jarvis/cli.py` and
`src/jarvis/ui/templates/` render it. `src/jarvis/ops.py` sits ABOVE those and routes the CLI.

Serena is activated for this repository and the code map is committed. Read the memories
`codebase-map`, `work-order-lifecycle` and `testing` instead of exploring the tree, and use
the symbol tools before grep. Do not spawn a subagent to rediscover the architecture.

`dispatch.bash_first_env` plus the `worker.bash_first` catalog key that feeds it is the
TEMPLATE every new switch here copies: a three-state string key, validated in `catalog.py`,
turned into an env dict by a small pure function in `dispatch.py`, splatted into the worker
settings, and reachable from `jarvis config set` through one row in `ops.py`. Read it before
you write a switch.

**Conventions.**

House style (`caveman` plus `i-have-adhd`) governs every byte you write, including PR bodies,
commit messages and code comments. A code comment is ONE line citing the spec section, never
the explanation. Test names are full sentences stating the behaviour. Reference files by
ABSOLUTE path in chat.

A switch is a catalog key under `worker.*`, resolvable fleet-wide and per project, NEVER a
module constant (`kn-f9a483d6`, `kn-1cec46b5`). A hook arm's POSITION in `preflight_decision`
is load-bearing, so state the position in the docstring and assert it in a test. New fields on
`ToolSpan` are ADDITIVE, following `backgrounded`. A reading that could not be taken renders
as `unclassified` or *not recorded*, never as `0` — absent is not zero, which is issue #227's
rule.

Work test-first: write the failing test, see it fail, then make it pass. Do NOT run the full
suite locally; it takes 21 to 22 minutes and blows the 5-minute prompt cache. Run the targeted
command your section names, then open the PR and cite CI (3.11, 3.12, 3.13 plus evals). Run
`uv sync --extra dev` first in a fresh worktree.

**Traps, each of which has bitten before.**

Classification of a Bash call reads the RAW `tool_use` input, not `detail` and not `params`:
`_detail_of` prefers the model's `description` over the `command`, and `ParamCaps` truncates
and drops keys.

The committed redacted transcript fixture has NO `"command"` key at all, so its 55 Bash calls
cannot be classified and must report `unclassified`, not zero.

A refusal placed AFTER the `is_jarvis_command_chain` auto-allow in `preflight_decision` is
UNREACHABLE in production however green its unit test is, because that auto-allow returns an
allow.

The Serena MCP server is `pending` for the first seconds of a session: sleep approximately 25
seconds before the first tool call, or a probe reads the tools as ABSENT falsely.

`~/.serena/serena_config.yml` holds 102 entries, one per Jarvis worktree, ALL named
`jarvis-os`. A fix that produces a 103rd `jarvis-os` has fixed nothing, and the failure mode
is silent: the worker gets some other worktree, or
`Error executing tool find_symbol: No active project.`

A PreToolUse hook judging shell structure on RAW text refuses prose; quoted spans must be
masked asymmetrically by quote kind (`kn-7f5f2d0d`). There is exactly one masker in the tree
and a second one is the defect that rule exists to prevent.

The DEFERRED paragraph in the worker brief becomes a FALSEHOOD the instant the tool-search env
flips. The flip and the reword are one commit, or every worker in the fleet is told to make a
`ToolSearch` call it does not need.

**What you must never do.**

Never answer this feature with louder prose. Never remove or narrow the `Bash` tool. Never
refuse a non-`.py` file or a genuine text search — those negative controls are required tests
and weakening one is a regression, not a stricter version of the feature. Never add a second
Serena MCP server. Never write anywhere under `~/workspace/production`. Never use bare
`git stash` or `git stash pop`: the stack is shared across more than 100 worktrees, so use a
WIP commit, or `git stash push -u -m "<unique-tag>"` plus `apply <sha>`. Never widen your
section's scope into a sibling's — ask Neo with `jarvis wo ask <wo-id> "…"` and end the turn.
Privileged actions (`pr_merge`, `release`, `service_restart`) are GATED: request one with
`jarvis gate request <wo-id> "<exact command>" --why … --evidence …`, end the turn, and never
retry a blocked command verbatim.
