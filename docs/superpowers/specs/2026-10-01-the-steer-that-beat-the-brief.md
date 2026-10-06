# The steer that beat the brief (issue #901, wo-52d757f7)

## The problem

Jarvis tells every worker "Serena first, grep second"
(`src/jarvis/worker_brief.py:556`, `navigation_section`). The fleet does not do it.

Measured over 276 Jarvis worker transcripts (kn-8107745e, io-edacb3ea):

| Tool | Calls, fleet-wide |
|---|---|
| `find_symbol` | 0 |
| `find_referencing_symbols` | 0 |
| `get_symbols_overview` | 0 |
| `Grep` | 0 |
| `Bash` | 24,356 |
| `grep` (inside Bash) | 4,271 |
| `sed -n` (inside Bash) | 3,902 |
| `cat` (inside Bash) | 999 |

Bash navigation over `.py` files is 41.3% of all conversation-body read volume
(6,019 MB of 14,558 MB); Bash navigation generally is 55.1% — the largest single cost
category in the fleet. The instruction has a 0% hit rate. Not a weak effect: zero.

The capability side is already correct and is NOT the cause (kn-a397fb52): the tools are
wired, `dispatch.serena_allow_rules()` (`src/jarvis/dispatch.py:52`) grants all eleven
read-only Serena tools under both prefixes, and `_write_worker_settings` merges them into
`permissions.allow` whenever `wiring.serena_wired` is true
(`src/jarvis/dispatch.py:91-105`). A worker that calls `find_symbol` succeeds. It never
calls it.

Three causes, all three confirmed on CLI 2.1.284.

### 1. The bash-first steer, appended AFTER the Jarvis brief

`dispatch` resolves `permission_mode` from the catalog (`src/jarvis/dispatch.py:1302`,
default `auto` — `catalog.DEFAULT_PERMISSION_MODE`, `catalog.py:25`). Auto mode appends a
system-prompt block of the CLI's own, after Jarvis's `--append-system-prompt`. Verbatim,
strict variant, from the 2.1.284 binary:

> Do your work through the Bash tool wherever it can accomplish the job: read files with
> cat, head, or sed -n, search with grep and find, and make file changes with sed,
> heredocs, or short scripts, rather than using the dedicated Read, Edit, or Write tools.
> Fall back to a dedicated tool only when Bash genuinely cannot do the job.

Relaxed variant:

> Prefer the Bash tool when it is the simpler route: read files with cat, head, or sed -n,
> search with grep and find, and make small, mechanical file changes with sed, heredocs,
> or short scripts instead of the dedicated tools.

That is the measured tool mix, item by item: `cat`, `head`, `sed -n`, `grep`, `find`. The
fleet is obeying an instruction. Not Jarvis's.

**This is the root cause.** Causes 2 and 3 are why nothing in the brief could have won
anyway, and both are also fixed here.

#### kn-8107745e is wrong about this, and the correction has to be on the record

That entry states no setting turns bash-first off. False. Two undocumented environment
variables are present in the 2.1.284 binary:

* `CLAUDE_CODE_THRIFTY_SONIC` — whether bash-first is on at all. Minified source:
  `function uNt(){if(a.CLAUDE_CODE_THRIFTY_SONIC!==void 0)return a.CLAUDE_CODE_THRIFTY_SONIC;switch(Wr()){case"forced":return!0;case"none":return!1;case"cohort":return x(Po,!1)}}`
  — the env value wins over the statsig cohort when defined.
* `CLAUDE_CODE_COZY_TEAPOT` — the steer variant. `function bo(e){return e==="strict"||e==="relaxed"?e:void 0}`
  and `function sHo(){return a.CLAUDE_CODE_COZY_TEAPOT??Ce().bashFirstSteerVariant()}`.
  Default `strict`; only those two strings validate.

How that was established, so the next reader can repeat it rather than trust it. Live
probes, opus, `claude -p --permission-mode auto`, reading the `attachment` records of type
`auto_mode` out of the session transcript under `~/.claude/projects/`:

| Probe cwd | Env | `auto_mode` attachment |
|---|---|---|
| `/tmp/bf4` | none | `{"type":"auto_mode","bashFirst":true,"bashFirstSteer":"relaxed","steerOnly":true,"bypass":false}` |
| `/tmp/bf5` | `CLAUDE_CODE_COZY_TEAPOT=relaxed` | same, `relaxed` |
| `/tmp/bf6` | `CLAUDE_CODE_THRIFTY_SONIC=false` | **none at all** — and the string `auto mode is active` appears nowhere in the transcript |

The ledger is append-only, so the correction needed a command, not a spec edit: during
this work order the lead retracted `kn-8107745e` and filed `kn-40d0ba16` with the
corrected claim. Done, not pending.

Also recorded because it changes how the defect reads: this work order's own session
attachment says `"bashFirstSteer":"strict"` while the unset `/tmp/bf4` baseline said
`"relaxed"`. The variant is a per-session statsig cohort draw. Today a worker's steer
strength is luck, which is why the measured behaviour varies between orders and why no
amount of brief-rewording would have been reproducible.

### 2. Serena is deferred behind `ToolSearch` on this CLI

The symbol tools are announced to the model as bare names with no schema, resolved on
demand through `ToolSearch`. So the opening conditional of `worker_brief.navigation_section`
—

> If this project has Serena (its symbol tools appear in your tool list, or
> `.serena/project.yml` is in the repo), use it to find code…

— reads FALSE at the moment the worker reads it. Jarvis's own instruction hands the worker
a test it fails. Measured: 302 `ToolSearch` calls fleet-wide, 8 of which ever selected a
Serena tool.

### 3. No ordinary lead is ever told the full posture

`worker_brief.core_contract` (`src/jarvis/worker_brief.py:203`) ships no navigation text at
all. The lead gets one index line — `SECTION_HOOKS["navigation"]`,
`src/jarvis/worker_brief.py:161` — pointing at `jarvis brief navigation`, a command it must
choose to run. The full `navigation_section` reaches a planner and an analyst inline
through `dispatch._common_briefing` (`src/jarvis/dispatch.py:433`). An ordinary work
order's lead reaches it only by fetching it, and the measured fetch behaviour is that it
does not.

## The fix

Four parts. §1 and §2 are settled decisions — Neo answered both design questions on this
work order — not options.

### 1. `worker.bash_first`: a catalog key, fleet-wide with per-project override

New `WorkerDefaults` field (`src/jarvis/catalog.py:407-430`), a STRING ENUM, not a
boolean:

```python
VALID_BASH_FIRST = ("off", "relaxed", "strict", "cli")
DEFAULT_WORKER_BASH_FIRST = "off"
```

| Value | Env written into the settings file | Means |
|---|---|---|
| `off` (default) | `CLAUDE_CODE_THRIFTY_SONIC="false"` | no steer, no `auto_mode` attachment |
| `relaxed` | `CLAUDE_CODE_THRIFTY_SONIC="true"`, `CLAUDE_CODE_COZY_TEAPOT="relaxed"` | the softer copy, pinned |
| `strict` | `CLAUDE_CODE_THRIFTY_SONIC="true"`, `CLAUDE_CODE_COZY_TEAPOT="strict"` | the worst case, pinned |
| `cli` | neither key written | leave the CLI alone; the cohort draw decides |

A bare boolean cannot express `cli`, and `cli` is the state that matters for honesty: it is
the only value under which Jarvis is NOT asserting an answer about a vendor behaviour it
does not own. `strict` exists for one reason and it is not symmetry — §4's behavioural eval
needs an arm that reproduces the steer deterministically, and an arm whose strength comes
from a statsig cohort draw is not a measurement.

**The default DISABLES, it does not relax.** `relaxed` is a softer copy of the same
instruction that already beat the brief at 100%; nothing suggests a weaker version of a
winning instruction loses. The posture has a measured 0% hit rate against the strict steer,
so the default is the state with no steer in it, and `relaxed` stays one `jarvis config set`
away for whoever re-measures Serena uptake later.

Parse in `parse_catalog`'s per-project block, exactly as `permission_mode` already does it
(`src/jarvis/catalog.py:2127-2130`) — that is the existing fleet-wide-with-override idiom
and it sits five lines from where the new key goes:

```python
bash_first = w.get("bash_first", os_cfg.default_bash_first)
if bash_first not in VALID_BASH_FIRST:
    raise _err(f"project {name}: worker.bash_first {bash_first!r} not in "
               f"{sorted(VALID_BASH_FIRST)}")
```

plus `OsConfig.default_bash_first = DEFAULT_WORKER_BASH_FIRST` read from
`os.defaults.bash_first`, validated beside the `os.defaults.permission_mode` check at
`src/jarvis/catalog.py:2098`.

`jarvis config set <project> worker.bash_first relaxed` needs no further code: `ops.set_config`
(`src/jarvis/ops.py:11083`) writes any dotted path and re-parses the document to validate,
so the enum check above IS the error message the user sees. Add one `APPLY_RULES` entry
(`src/jarvis/ops.py:10689`), `("*.bash_first", "next-dispatch")` — the value is read once
per spawn into the settings file, exactly like `*.wiring.*` two lines above it, and a
running worker's session already holds the system prompt it was launched with.

### 2. Where the env is written: `dispatch._write_worker_settings`

Into the `env.update({...})` block, `src/jarvis/dispatch.py:140-224`, beside
`MCP_TOOL_TIMEOUT`. Three reasons it goes there and not in `assets/settings.base.json`:
that `env.update` beats both the asset and the project's `settings_overrides`, so a value
carried by the asset could not be overridden from the catalog (the comment at
`src/jarvis/dispatch.py:196-203` already argues this for `MCP_TOOL_TIMEOUT`); the settings
file is what travels to a worktree where the project's own `.claude/settings.json` does not
exist; and it reaches EVERY session the spawn produces, including the crew seats, which is
what makes §3 hold rather than merely ask.

Two constraints on the implementation:

* **Every value in that dict must be a `str`.** Claude Code's `env` is a
  `Record<string,string>`; an integer risks the CLI rejecting the whole settings file. The
  probed working value is the literal `"false"`.
* **All four catalog values are now PROBED.** `"false"` was already (`/tmp/bf6`, above:
  no `auto_mode` attachment at all). `CLAUDE_CODE_THRIFTY_SONIC="true"` was inferred and
  no longer is: `/tmp/bf7`, with
  `CLAUDE_CODE_THRIFTY_SONIC=true CLAUDE_CODE_COZY_TEAPOT=strict claude -p … --model opus
  --permission-mode auto`, read back an `auto_mode` attachment of
  `{"bashFirst":true,"bashFirstSteer":"strict","steerOnly":true,"bypass":false}` — so
  `"true"` coerces as expected AND the variant key pins the strict copy. With `/tmp/bf4`
  (no env, `relaxed`) and `/tmp/bf5` (`COZY_TEAPOT=relaxed`, `relaxed`) that covers `off`,
  `relaxed`, `strict` and `cli`. The harness test in §5.1 still asserts the STRING
  WRITTEN, never CLI behaviour; that distinction is deliberate and must survive review —
  the probe is how the string is known to be right, not something CI can re-run.

The steer only exists under `auto`, so writing the keys under `acceptEdits`/`default` is a
no-op. Write them unconditionally anyway: a per-mode conditional is a second source of
truth about a vendor behaviour, and `permission_mode` is overridable per work order
(`src/jarvis/dispatch.py:1302`).

### 3. The navigation block is INLINED into the bare worker prompt

New `worker_brief.navigation_core(serena: bool) -> list[str]`, composed in
`dispatch.build_worker_prompt` (`src/jarvis/dispatch.py:393-399`) as its OWN top-level
block AFTER `worker_brief.section_index`, before the pre-approval marker. Emitted only when
`wiring.serena_wired(project.wiring)`; omitted entirely otherwise, where the index already
swaps in `NO_SERENA_HOOK` (`src/jarvis/worker_brief.py:319`) and the fetched section already
carries the grep posture.

**Why after the index and not inside `# Operating contract`.** Measured: the core is 3862
chars against `worker_brief.CORE_BUDGET_CHARS` 3900 (`src/jarvis/worker_brief.py:59`), and
the bare prompt 4911 against the `< 5000` assertion at `tests/test_worker_brief.py:84`. 38
and 89 chars of headroom; this block costs 400-600. Placing it after the index raises ONLY
the 5000 assertion and leaves the core-budget story untouched — and the core's provenance
is the reason that matters more than the arithmetic: every sentence in it survived
`evals/llm/test_worker_contract_ab.py`, and dropping 500 ungraded chars in there dilutes a
claim the budget exists to protect.

The block must do four things:

1. **Say the tools are DEFERRED, not absent, and give the exact recovery call** — one
   `ToolSearch` select line, printed verbatim below and asserted by test.

   Four tools, not eleven: these are the ones the defect is about, and a longer select is
   more chars for tools the measurement says nobody wanted.

   **Both prefixes, in ONE select line — eight names.** `mcp__serena__` comes from
   `claude mcp add serena`, `mcp__plugin_serena_serena__` from a plugin install
   (`dispatch.SERENA_TOOL_PREFIXES`, `src/jarvis/dispatch.py:49`), and Jarvis configures
   no MCP server itself so it cannot know which this install has. PROBED on 2.1.284:
   `ToolSearch` with
   `select:mcp__serena__find_symbol,mcp__plugin_serena_serena__find_symbol` on a plugin
   install returned ONLY `mcp__plugin_serena_serena__find_symbol` — the absent short name
   was silently ignored and the rest of the select survived. That is the same rule
   kn-a397fb52 cause 3 established for a `tools:` list, now confirmed for a select. So the
   line names both spellings of all four tools and there is NO retry sentence: the
   uncertainty it paid for is gone. The literal rendered line, which the harness test
   asserts:

   ```
   select:mcp__serena__find_symbol,mcp__plugin_serena_serena__find_symbol,mcp__serena__find_referencing_symbols,mcp__plugin_serena_serena__find_referencing_symbols,mcp__serena__get_symbols_overview,mcp__plugin_serena_serena__get_symbols_overview,mcp__serena__activate_project,mcp__plugin_serena_serena__activate_project
   ```

   It costs 325 chars rather than the ~165 a one-prefix line would, which is why the
   prompt-size bound in §5.1 item 3 lands at 5900 and not 5600.

2. **Say the bash-first reminder does NOT govern code navigation.** Belt and braces on §1:
   it survives a project setting `relaxed` or `cli`, and it survives the per-session cohort
   draw if a future CLI re-introduces the steer under a different name.

3. **Drop cause 2's broken conditional.** No "if its symbol tools appear in your tool
   list". A worker must never be told to check a thing that is false by design.
   `worker_brief.navigation_section`'s own opening sentence (`src/jarvis/worker_brief.py:590-592`)
   gets the same treatment — one defect, two renderings, and the fetched section is what a
   lead reads when it goes looking.

4. **Rank the three calls by what grep cannot do**, as the existing section does:
   `find_referencing_symbols` first (no grep equivalent), `get_symbols_overview` before
   opening a file whole, `find_symbol` instead of `grep -rn "def foo"`. One line each; this
   is not a tutorial.

### 4. `jarvis-implementer`: Serena for the WHOLE task, not once at the start

`src/jarvis/assets/worker-agents/jarvis-implementer.md`. The seat already has every Serena
tool in `tools:` (line 4) and an eleven-line opening section telling it to call Serena
first. Its heading is the defect: **"# Before anything else: how you look at code"**
(line 11) scopes the whole posture to the start of the task. Observed behaviour matches the
heading exactly — implementers activate Serena once, then switch to `cat`, `sed` and
`grep` for the rest of the task.

The asymmetry that identifies the cause: `jarvis-spec-writer`, which has **no `Bash`
tool** (`tests/test_worker_crew.py:81-86` holds that), makes 18-48 Serena calls per task.
Same instruction text, same project, same model. The seat with a shell relapses; the seat
without one cannot. That is the cleanest available evidence that **the steer, not the
instruction, decides** — and the reason §1 is the root-cause fix while this section is
reinforcement.

Exactly what changes:

* Heading becomes a standing rule, not a first step — e.g. `# How you look at code, for
  the whole task`.
* Add the relapse pattern by name: activating Serena once and then answering later symbol
  questions with `cat`/`sed -n`/`grep` is the failure, and it is a failure at minute 30 as
  much as at minute 1. Every symbol question, at any point, including after an edit and
  including when re-reading a file already opened.
* Add one line that the bash-first reminder does not govern code navigation, matching §3
  item 2 word for word so the lead and the seat cannot drift.
* Keep `tools:` unchanged.

**Constraint on the text: no prefixed tool name may appear in the seat BODY.**
`bootstrap._strip_serena` (`src/jarvis/bootstrap.py:109-115`) substitutes
`_SERENA_TOOL_ENTRY` out of the FRONT MATTER only, and `tests/test_worker_crew.py:89-95`
asserts neither `mcp__serena__` nor `mcp__plugin_serena_serena__` appears anywhere in an
unwired seat file. That passes today only because every seat body spells tools bare
(`activate_project`, `find_symbol`). A ToolSearch select line in the body would break the
unwired strip. So: bare names in the seat; the prefixed select line lives only in §3's
worker block, which is already gated on `serena_wired`.

### 5. How the fix is proven

Two layers. The free one holds the mechanism; the paid one holds the behaviour.

#### 5.1 Free harness (`tests/`, every CI run)

1. **Catalog** (`tests/test_catalog.py`): each of the four values parses; an invalid value
   raises `CatalogError` naming `worker.bash_first` and listing the valid values; a project
   value overrides `os.defaults.bash_first`; absent inherits the fleet default `off`.
2. **Settings file** (new `tests/test_bash_first.py`, or the dispatch block of
   `tests/test_pipeline.py`): for each catalog value, the exact env written —
   `off` → `CLAUDE_CODE_THRIFTY_SONIC == "false"` and no `CLAUDE_CODE_COZY_TEAPOT` key;
   `relaxed` → `"true"` + `"relaxed"`; `strict` → `"true"` + `"strict"`;
   `cli` → NEITHER key present in `settings["env"]`. Plus one general assertion worth its
   line: every value in `settings["env"]` is a `str`. That is the `Record<string,string>`
   rule as a test instead of as a comment, and it catches the next integer too.
3. **Prompt** (`tests/test_worker_brief.py`): the nav block is in the bare prompt; it
   appears AFTER `# Full briefings on demand`; the select line is present as the exact
   literal from §3; the "does not govern code navigation" sentence is present; the block is
   ABSENT when the project has Serena deselected; the core slice
   (`# Operating contract` … `# Full briefings`) is still under `CORE_BUDGET_CHARS`
   unchanged at 3900; and line 84's bound rises 5000 → 5900 with the measured before/after
   in the comment, as that line already does for its two previous raises. 4911 before,
   5846 after — 935 chars rather than the 400-600 §3 estimated, because the select line
   names both prefixes (§3 item 1) and that is 325 chars of tool names on its own.
4. **Seat** (`tests/test_worker_crew.py`): the implementer text carries the whole-task
   phrasing and the relapse words; and the new structural one — no `mcp__serena__` /
   `mcp__plugin_serena_serena__` substring in the seat BODY, which is the assertion that
   stops someone pasting the select line where `_strip_serena` cannot reach it.

#### 5.2 Behavioural (`evals/llm/test_navigation_judgment.py`, opt-in)

Extend that file. Do not build a second eval harness: it already grades TOOL CALLS through
a `PreToolUse` recorder hook, attributes them by `agent_type` (`seat_calls`, line 194), and
carries both anti-vacuity traps kn-a397fb52 documents — `search_for_pattern` excluded from
`symbol_tools` (line 67), and the negative control at line 284 letting a genuine text
question be answered by text search.

Two things it must gain, and the first is the one that matters:

1. **Two arms, steer present and steer disabled.** `run_and_record` hardcodes
   `permission_mode="acceptEdits"` (line 187), so **no arm of this eval has ever seen the
   bash-first steer** — the defect the fleet is dying of is invisible to the suite that
   exists to catch it. Give `run_and_record` a `permission_mode` parameter (defaulting
   to the `auto` dispatch spawns) and a `bash_first` one resolved through
   `dispatch.bash_first_env` — the catalog value, not a hand-typed env dict, so an arm
   cannot drift from what a real spawn writes — and parametrize the worker scenarios over:

   * `no-steer`: `auto` + `CLAUDE_CODE_THRIFTY_SONIC=false` — what §1 and §2 now write.
     Hard assertion: a symbol call present, no text-search-for-code.
   * `steer`: `auto` + `THRIFTY_SONIC=true` + `COZY_TEAPOT=strict` — pinned, never left to
     the cohort draw. **Recorded, not asserted** (`xfail` non-strict, or an assertion only
     that the arm ran). The eval must not fail CI on a vendor behaviour Jarvis does not
     control; its job here is to show the two arms differ, which is the measurement that
     `off` is the right default.

2. **A Bash-command classifier, or the "did not grep" check stays blind.**
   `TEXT_SEARCH_TOOLS = {"Grep", "Glob"}` (line 60) — and `Grep` was called ZERO times
   fleet-wide. The real failure is `Bash(grep …)`, which this eval cannot see. The
   `RECORDER` (line 122) must capture `tool_input.command` for Bash, and a classifier must
   count `grep`/`rg`/`sed -n`/`cat`/`head`/`find` aimed at `.py` paths as a
   text-search-for-code. The line 57-60 comment ("`Bash` is not here: a worker runs all
   sorts of legitimate shell") was right about the TOOL and wrong about the COMMAND — the
   fix is to classify the command string, not to start failing every Bash call. The
   negative control at line 284 already permits `Bash` and stays exactly as it is: it is
   scoped by the QUESTION being a text question, not by the tool.

#### 5.3 The before/after counts the issue asks for

**BEFORE is the table in "The problem"** — kn-8107745e's figures over 276 transcripts.

**AFTER cannot be measured inside this work order.** It requires the change live on the
fleet for long enough to accumulate transcripts. Stating that plainly is the honest answer;
quoting any after-figure from this worktree would be quoting the eval, not the fleet.

The method, written down so a follow-up reproduces the same numbers against the same
denominators:

1. Corpus: worker session transcripts under `~/.claude/projects/*/*.jsonl`, filtered to
   Jarvis-dispatched sessions.
2. Tool mix: every `tool_use` block in `assistant` messages, counted by `name`. For
   `name == "Bash"`, classify `input.command` into navigation (`cat`/`head`/`sed -n`/
   `grep`/`rg`/`find`) versus other, and separately whether the target path ends `.py`.
3. Read volume: bytes of `tool_result` content in `user` messages, attributed to the
   `tool_use` that produced it. "Conversation-body read volume" is the denominator —
   14,558 MB in the before run. Report shares, not raw megabytes, so a differently sized
   corpus stays comparable.
4. Report: symbol calls versus Bash-navigation calls, and Bash-navigation-over-`.py` as a
   share of read volume. Before: 0 symbol calls, 41.3%.

File it as a follow-up work order once the change has shipped and the fleet has run on it.
Not a blocker on this order, and not something this order can fake.

## Rejected alternatives

1. **Strengthen the brief; change no settings.** The obvious fix, and it is the thing
   already in production. Measured 0% hit rate. The steer is the vendor's own system-prompt
   block appended AFTER Jarvis's, it names the exact tools the fleet reached for, and its
   strength varies per session by cohort draw — so even a win would not be reproducible.
   Rewording loses to a later instruction, every time.
2. **Switch workers off `auto` to escape the steer.** Buys the navigation fix by breaking
   autonomy. `dispatch`'s own comment (`src/jarvis/dispatch.py:84-92`) records why `auto`
   is the default: under `acceptEdits`/`default` a headless session prompts and stalls,
   verified live. Changing what a worker may DO in order to change how it READS is the
   wrong trade, and `permission_mode` is a user-facing catalog setting this would quietly
   commandeer.
3. **Hardcode the env in `dispatch` as a module constant.** Neo's condition rules it out,
   and the reason is right: re-measuring Serena uptake under `relaxed` would then need a
   code change and a release. A catalog key makes the re-measurement a `jarvis config set`.
4. **`bash_first: true | false`.** Cannot express "leave the CLI alone". Jarvis would be
   asserting an answer in every state, including the state where it has none.
5. **Withhold `Bash` and `Grep` from the lead, as `jarvis-spec-writer` does.** The seat
   trick does not transfer: a lead needs Bash for every `jarvis …` command, the test suite,
   and git. §4's asymmetry is evidence about the cause, not a template for the fix.
6. **Undefer Serena so the tools appear in the tool list.** No setting was found that
   opts an MCP server out of `ToolSearch`, and asserting one exists without a probe is how
   kn-8107745e got its false claim. The select line costs one call and is verifiable today.
7. **Put the nav block inside `# Operating contract`** or raise `CORE_BUDGET_CHARS`. 38
   chars of headroom, and the budget is the cost control that forced the core/sections
   split in the first place; raising it to fit the next good idea is how it stops meaning
   anything. The core is also A/B-graded as a unit — 500 ungraded chars in there weaken a
   claim, not just a number.

## Not in scope

* The AFTER measurement (§5.3) — needs the change live on the fleet.
* `jarvis-architect` and `jarvis-test-lead`. Already graded by
  `evals/llm/test_navigation_judgment.py:210-237` and not implicated by the measurement.
* The deferral itself. A vendor behaviour; §3 routes around it.
* `search_for_pattern` policy. Unchanged, and deliberately still excluded from
  `symbol_tools`.

## Open questions

Both of the questions this spec opened are now CLOSED by probe, and the sections above are
written to the answers rather than around them.

* Whether an unknown tool name in a `ToolSearch` select is inert, as it is in a `tools:`
  list. **Yes** — probed on 2.1.284 (§3 item 1). Both prefixes ride in one select line and
  the retry sentence is dropped.
* Whether `CLAUDE_CODE_THRIFTY_SONIC="true"` does what `"false"` does in reverse. **Yes** —
  probed at `/tmp/bf7`, `auto_mode` read back `bashFirst:true` with
  `bashFirstSteer:"strict"` (§2). All four catalog values are probed; no value ships on
  inference.
