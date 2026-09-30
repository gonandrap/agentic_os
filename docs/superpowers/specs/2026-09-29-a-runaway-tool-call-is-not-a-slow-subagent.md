# A runaway tool call is not a slow subagent

Work order wo-4f75af18, GitHub issue #845. Evidence in §1 is the issue's and its owner's,
taken as verified.

## The problem

### 1. Eighteen minutes on one MCP tool call, and nothing in the OS noticed

wo-37f2fc2c's `jarvis-spec-writer` subagent called Serena `search_for_pattern` with a
multi-line pattern of nested dot-star groups over a 2659-line file. Serena matches with
`re.DOTALL`; the regex backtracked catastrophically. The Serena MCP server sat at ~87% CPU
and accumulated 18m50s of CPU time. The call never returned.

The lead's transcript:

| 06:59:58Z | the `Agent` `tool_use` block is written |
| 07:25:18Z | its `tool_result` finally lands |

25 minutes of lead wall clock, of which ~19 were one hung tool call inside the subagent.
Nothing raised anything. Five separate mechanisms each had a reason not to:

**1a. A foreground `Agent` is not classified as a wait.**
`src/jarvis/inspection.py:107-110` declares `JOIN_TOOLS = ("TaskOutput",)` and its comment
says why: "`Agent` itself returns immediately when the subagent is backgrounded, and the
wait it defers is exactly what `TaskOutput` later collects." True of `run_in_background:
true`. **False of a foreground `Agent`/`Task`, which blocks the lead until the subagent
returns** — and every crew delegation in this fleet is foreground. So
`ToolSpan.is_join` (`src/jarvis/inspection.py:492-493`) is `False` for it.

**1b. Which makes the alarm loop skip the span it needed.**
`read_transcript` DID create the span, open, with `ended=0.0`
(`src/jarvis/inspection.py:1102-1118`; a `ToolSpan` with no matching `tool_result` is
`finished == False` by `:484-486`). The alarm loop at `src/jarvis/inspection.py:1614-1623`
tests `span.is_join and not span.finished`, so it walked straight past it. `long-join` was
unraisable for a foreground delegation.

**1c. The same misclassification is a wrong REPORT, not only a missed alarm.**
`Turn.blocked` sums `is_join` spans and `Turn.tools` sums the rest
(`src/jarvis/inspection.py:620-626`). So every foreground delegation the fleet has ever
made was counted as the lead RUNNING A TOOL. `jarvis inspect` printed "blocked on a
subagent 0%" (`cli.PART_LABELS["blocked"]`, `src/jarvis/cli.py:1971`) for wo-37f2fc2c, and
`Anatomy.joins()` (`src/jarvis/inspection.py:869-873`) has never listed one.

**1d. Nothing else covers the case.** Each refusal is correct on its own terms:

- `long-turn` needs `alarm_turn_minutes * 60` of ACTIVE time — 60 minutes
  (`catalog.py:DEFAULT_INSPECT_ALARM_TURN_MINUTES`). 25 minutes is under it.
- `slow-model-response` needs `turn.awaiting` (`src/jarvis/inspection.py:1605`), and
  `awaiting_since` is RESET to the `tool_use`'s row time — a turn whose last row is a
  `tool_use` is not awaiting the model, it is waiting for a tool.
- `alarms()` judges the LAST TURN of the MAIN transcript only
  (`src/jarvis/inspection.py:1577`). Subagent anatomies are read for reports
  (`_attach_subagents` at `:1256-1260`, `_subagent_transcripts` at `:1721-1729`,
  `SUBAGENT_DEPTH_READ = 1`) and **never for alarms**.
- `worker_session.TURN_STALL_SECONDS` is 6h and judges the PROCESS, which was alive and
  burning CPU.

**1e. Measured consequence.** `jarvis alarms` fleet-wide: **zero** `long-join` alarms ever
raised. 272 foreground `Agent` calls exist across worker transcripts; 43 lasted 20+
minutes, the longest 71 (wo-da90f283). Most of those 43 are presumably real subagent work
— which is the reason the fix in §3 is not "alarm on elapsed wait".

### 2. Nothing bounds one MCP tool call

Claude Code reads `MCP_TOOL_TIMEOUT` (milliseconds) from the process environment. The
worker's settings `env` block (`src/jarvis/assets/settings.base.json:3-5`, plus the
per-work-order additions at `src/jarvis/dispatch.py:122-190`) does not set it. So a hung
MCP server call has no ceiling at all: the 18m50s above ended when the work order ended,
not when anything decided it had gone on long enough.

### 3. The pattern that hung it is still writable, and the hook that would refuse it cannot fire

The `PreToolUse` matcher in `src/jarvis/assets/settings.base.json:12` is
`"Bash|Edit|Write|NotebookEdit"`. No MCP tool name matches it.

That has a second, unrelated consequence worth recording: `preflight_decision` has an
`mcp__` branch (`src/jarvis/hooks.py:1307-1309`) routing to
`investigator_write_decision`, whose own `mcp__` fail-closed rule
(`src/jarvis/hooks.py:760-768`) is therefore **dead in production** — no MCP call reaches
the hook at all. That is a separate defect; see §7.

## The fix

Four changes. Nothing here kills a process, and no new alarm KIND is added.

### 1. A foreground `Agent`/`Task` span IS a join

**Where:** `src/jarvis/inspection.py`, the constants at `:107-117`, `ToolSpan` at
`:459-502`, and the span construction in `read_transcript` at `:1102-1118`.

Mechanism:

```python
#: Tools whose span is the lead DELEGATING and then waiting for the result. A foreground
#: call blocks the lead until the subagent returns; only `run_in_background: true` makes
#: `Agent` return immediately, and that is what `TaskOutput` later collects.
DELEGATION_TOOLS = ("Agent", "Task")
JOIN_TOOLS = ("TaskOutput",)          # unchanged: always a wait, background or not
```

`ToolSpan` gains one field and `is_join` becomes:

```python
#: `run_in_background: true` on the call that opened this span. Read from the RAW
#: `tool_use` input, never from `params`: those are redacted strings capped by
#: `ParamCaps`, so a dropped key would silently reclassify a join.
backgrounded: bool = False

@property
def is_join(self) -> bool:
    if self.name in JOIN_TOOLS:
        return True
    return self.name in DELEGATION_TOOLS and not self.backgrounded
```

`read_transcript` sets it at `:1108-1113` from `bool((block.get("input") or {}).get(
"run_in_background"))` — the same raw-block read `background._scan_calls`
(`src/jarvis/background.py:144-150`) already does, and for the same reason.

**Why there.** `is_join` is the single predicate `blocked`, `tools`, `joins()`,
`_name_joins` and the alarm loop all consult. One property, five corrected readers. Doing
it in the alarm loop alone would leave §1c — the wrong report — in place.

**What this does to existing reports** (state it, because it is a visible change, not a
silent one):

- `Turn.blocked` / `Turn.tools` re-partition. A turn's seconds move from `running tools`
  to `blocked on a subagent`. `PARTS` is unchanged, the percentages still sum to 100, and
  `cli.PART_LABELS` / `PART_SHORT` / `BAR_GLYPHS` keep the same keys — so no renderer
  changes shape. The numbers inside them change, and for wo-37f2fc2c they change from
  wrong to right.
- `Anatomy.joins(over)` starts returning foreground `Agent` spans at or over
  `report_join_floor` (30s, `catalog.py:588`). Consumers —
  `ui/templates/_debug_anatomy.html:122-126` and `jarvis inspect`'s joins section — read
  a list and a name; a longer list needs no change. `report_join_floor` itself is
  untouched: 30s is still "worth a line", and a delegation shorter than 30s still is not.
- `_name_joins` (`:1385-1390`) now also visits `Agent` spans. Its guard is `span.detail in
  labels`, and an `Agent` span's `detail` is the call's `description`, never a task id
  (`_names`' docstring at `:1351-1362`), so no `Agent` detail is rewritten. No change.

**Test:** a fixture with one `Agent` span, foreground, finished — `span.is_join is True`,
it appears in `Anatomy.joins(0)`, and its seconds are in `Turn.blocked` and not in
`Turn.tools`. The same span with `"run_in_background": true` in its input —
`is_join is False`, and it is in `tools`.

### 2. The join alarm on a delegation span is gated on HANG EVIDENCE, not elapsed wait

**Decision recorded:** Neo, answering for the user, chose this over the issue's literal
reading ("alarm when a foreground `Agent` has been open past `alarm_join_seconds`"). At
`alarm_join_seconds = 300` (`catalog.py:646`) the literal reading fires on 43 of 272
foreground delegations — roughly one in six — and most of those 43 are real subagent work.
That trains the user to ignore the alarm, and an ignored cost alarm is worse than none
(the standing argument in `alarms()`' own docstring at `:1548-1550`). The cache cost of a
long delegation is real, and it is the cost of delegating at all.

**Where:** `src/jarvis/inspection.py`, the join loop at `:1614-1623`, plus one new helper
beside `_read_subagent`.

Two rules, not one:

**2a. `TaskOutput` keeps the existing unconditional rule, byte for byte.** It is a pure
wait with no subagent transcript of its own to inspect, and `alarm_join_seconds` is the
principled 5-minute cache TTL (`catalog.py:636-646`). Unchanged.

**2b. An open span whose `name in DELEGATION_TOOLS` raises `JOIN_ALARM` only when the
SUBAGENT's own anatomy shows a hung tool call.** The predicate:

```python
def hung_subagent_call(subs, now, older_than):
    """The first (subagent, span) whose LAST span is an unfinished tool call older than
    `older_than` seconds. `None` when every subagent is working normally."""
```

- "last span" is `sub.turns[-1].spans[-1]` — the thing it is doing right now. An
  unfinished span EARLIER in the transcript is a killed call the subagent already moved
  past, not a hang.
- `not span.finished and now - span.started >= older_than`.
- The subagent is considered only if that span started at or after the open delegation
  span's `started`, which keeps a previous turn's leftovers out.

**Threshold:** new catalog setting, following `catalog.py:774-792` and `_parse_inspect`
(`catalog.py:1521-1556`) exactly:

```python
#: A subagent's last tool call still unfinished after this long — the evidence that turns
#: an open foreground delegation from "a subagent is working" into "something is hung"
#: (issue #845). FIVE MINUTES, and the number is not free-standing: it is the same
#: boundary as `DEFAULT_INSPECT_ALARM_JOIN_SECONDS` (300) and the same as the
#: `MCP_TOOL_TIMEOUT` §4 puts in every worker's environment, so the OS holds ONE opinion
#: about how long a single tool call may take. The issue asked for "well before 60 min"
#: — `alarm_turn_minutes`, the only thing that would eventually have fired — and this is
#: twelve times earlier than that. No legitimate Serena or context7 call in this fleet's
#: transcripts takes a minute; the hang in #845 took nineteen.
DEFAULT_INSPECT_ALARM_SUBAGENT_TOOL_MINUTES = 5
```

`InspectConfig.alarm_subagent_tool_minutes: int`, a parse line in `_parse_inspect`, and
`* 60` at the read site (60 is already in the magic-number guard's allowed set —
`tests/test_inspection.py:805`). Refused below 1 by the existing loop at
`catalog.py:1557-1567`. In MINUTES, not seconds, because every other "how long may this
run" setting on `InspectConfig` is in minutes; `alarm_join_seconds` is in seconds because
it is a cache TTL.

**2c. The alarm text NAMES THE HUNG CALL.** Neo: "make sure the alarm names the hung tool
call, which is what #845 asks for." Shape:

```
subagent jarvis-spec-writer (a7b62083) blocked 19m in
mcp__serena__search_for_pattern (r"^def .*\n(.*\n)*?.*return") — long enough to lose the
prompt cache, so the wait will be paid for twice — `jarvis inspect wo-37f2fc2c`
```

- The subagent is `sub.label or sub.task_id` (`SubagentAnatomy.label`, from the
  `.meta.json` Claude Code writes; `_subagent_labels`).
- The tool is `span.name`, verbatim.
- **The input excerpt is `span.detail` and nothing else.** `_detail_of`
  (`src/jarvis/inspection.py:974-991`) already produces exactly this: it prefers
  `description`, `command`, `task_id`, `file_path`, `pattern`, `skill` in that order — so
  for `search_for_pattern`, which carries no description, it yields the PATTERN — and it
  redacts with `redact_param` BEFORE cutting to `cfg.quote_chars` (140,
  `catalog.py:592`). That is the bound, that is the redaction (the standing rule the
  brief cites as kn-09c5ead7: bound every field you did not write), and no new
  truncation or redaction path is introduced. `span.params` is deliberately NOT used: its
  per-value cap is 500 characters (`ParamCaps.per_value`), which is a REPORT bound, and an
  alarm reason is read in an inbox row and a Telegram push.
- Empty `detail` renders as the tool name alone. No `(…)` with nothing in it.

**2d. What `live_alarms` must now read that it did not.** `alarms()` takes an `Anatomy`
and `read_session` already attaches `SubagentAnatomy` objects to each turn
(`:1256-1260`) — **so `alarms()` needs no new file read at all; `Turn.subagents` is
already populated.** What changes is the COST of the `read_session` that `live_alarms`
performs (`:1637-1658`), because that read walks `_subagent_transcripts` unconditionally
today. Two facts the spec has to own:

- **It already pays this.** `live_alarms` → `read_session` → `_attach_subagents` →
  `_read_subagent` per subagent file, once per reconcile tick per running work order, in
  `Daemon.check_burning_turns` (`src/jarvis/daemon.py:4060-4136`). The reason the old code
  did not alarm on it is NOT cost — it is that nothing looked at the objects.
- **It is still bounded, and the bound is stated.** A worker's session directory holds one
  `subagents/*.jsonl` per delegation for the life of the work order, so the read grows
  with the number of subagents the order has spawned, not with the number that are
  running. `read_session` must therefore skip a subagent transcript whose **mtime is older
  than the open delegation span's `started`** — a `stat()` per file instead of a parse,
  the same cheap-pre-filter trick `_mentions`/`ONE_HOUR_KEY` uses at `:1713-1719`. In the
  measured shape (wo-37f2fc2c: 3 subagent files, one live) that is 3 `stat()`s and 1
  parse. This filter belongs in `_read_subagent`'s caller and must be keyed off a
  parameter, not a literal, so `ops.inspect_report` — which wants every subagent — passes
  nothing and keeps today's behaviour byte for byte.

**Why in `alarms()` and not in the daemon.** The daemon writes rows and flags attention; it
holds no opinion about what is wrong. Every other threshold in this feature lives in
`alarms()`, and `check_burning_turns` needs no edit at all: the new alarm is an existing
KIND (`long-join`), so its dedupe (`already`, keyed on kind+seq), its `cost_alarm` event,
its `wo_alarms` row and its attention flag all work unchanged.

**Tests** (the three the issue names, plus two):

1. An open foreground `Agent` span with `now` 301s after its `started`, and a subagent
   whose spans are all finished, raises **nothing**. This is the regression guard for the
   43 normal delegations.
2. The same span with `"run_in_background": true` is **not a join at all** —
   `span.is_join is False` — so the loop never considers it.
3. The same open foreground span, with a subagent whose last span is an unfinished
   `mcp__serena__search_for_pattern` started `alarm_subagent_tool_minutes * 60 + 1`
   seconds ago, raises exactly one `long-join`, and its `reason` contains
   `"mcp__serena__search_for_pattern"` and the subagent's label.
4. `TaskOutput` open past `alarm_join_seconds` with no subagent evidence still raises —
   rule 2a is unchanged.
5. `alarm_subagent_tool_minutes` is settable fleet-wide and per project, inherits
   field-by-field, and is refused at zero (`tests/test_inspection.py`'s three existing
   config shapes at :831 onward).

### 3. A per-tool-call MCP timeout in the worker environment

**Where:** the value lands in the worker settings `env` block written by
`dispatch._write_worker_settings` (`src/jarvis/dispatch.py:57-191`); the fleet default and
the per-project override live in `catalog.WorkerDefaults` (`catalog.py:372-392`) and its
parse block (`catalog.py:1976-2010`).

Mechanism, checked against the code:

- **The path that builds the file** is `dispatch._write_worker_settings`. It calls
  `bootstrap.build_settings(project.settings_overrides)` (`bootstrap.py:220-227`), which
  reads `assets/settings.base.json`, substitutes `__JARVIS_HOOK_CMD__` and deep-merges
  `settings_overrides`; then `dispatch` merges `wiring.settings_patch`, then it builds
  `env = dict(settings.get("env") or {})` at `:122` and `env.update({...})` at `:123-189`,
  and writes `settings["env"] = env` at `:190`. **So the `env.update` block is the merge
  point and it WINS over both `settings.base.json` and `settings_overrides`.** The
  catalog-driven value must therefore be set there, not in the asset — otherwise a project
  could not raise it.
- **`settings.base.json` carries the fleet default as documentation** only if it is left
  out of the `env.update`; it must not be, or the catalog override is unreachable. So:
  `settings.base.json:3-5` is left alone, and the single source is
  `env["MCP_TOOL_TIMEOUT"] = str(project.worker.mcp_tool_timeout_ms)`.
- **The env value MUST be a string.** Every value in that dict is a string —
  `settings.base.json` has `"JARVIS_MANAGED": "1"`, and `dispatch` already wraps numbers
  (`"JARVIS_SUMMARY_MAX_WORDS": str(...)` at `:152`). Claude Code's settings `env` is a
  `Record<string,string>`; an integer there is a settings file the CLI may reject
  wholesale, which would take the hooks and permissions down with it.
- Catalog: `DEFAULT_WORKER_MCP_TOOL_TIMEOUT_MS = 300_000` beside the other worker
  defaults, field `mcp_tool_timeout_ms: int` on `WorkerDefaults`, parsed as
  `int(w.get("mcp_tool_timeout_ms", DEFAULT_WORKER_MCP_TOOL_TIMEOUT_MS))` in the
  `require_crew` neighbourhood (`catalog.py:1982-2009`) and **refused below 1000** with a
  message naming the key — a millisecond value under a second is a typo that would make
  every MCP call fail, and it arrives through `jarvis config set`.

**Rationale for 300000.** Five minutes is far above every legitimate Serena/context7 call
in this fleet's transcripts (all sub-minute) and it is the OS's existing cost boundary —
the 5-minute prompt-cache TTL that `claude_cli.PROMPT_CACHE_5M_ENV` buys and that
`DEFAULT_INSPECT_ALARM_JOIN_SECONDS` is set to. A call that outlives the cache has already
cost the conversation a re-write; letting it run further buys nothing. A project that
genuinely has a slow MCP server RAISES it, which is why it is a catalog setting and not a
literal.

**Why a timeout does not make §2's alarm redundant** (a reviewer will ask): the timeout
bounds an MCP call in a process Jarvis launched. It does not bound a non-MCP tool, it does
not apply to a session a user started and injected (`jarvis wo inject`), and a project that
raises it to an hour has bought back the exact hazard. The alarm reports; the timeout
prevents. Neither substitutes.

**Tests:** `_write_worker_settings` writes `env["MCP_TOOL_TIMEOUT"] == "300000"` by
default; a catalog `worker.mcp_tool_timeout_ms: 900000` for one project produces
`"900000"` for that project's workers and `"300000"` for another's; a project's
`settings_overrides` setting the same key does NOT win (the `env.update` precedence above,
asserted so the precedence is pinned rather than incidental); `mcp_tool_timeout_ms: 5` is
refused by `parse_catalog` with the key in the message.

### 4. A `PreToolUse` hook that refuses a catastrophic `search_for_pattern`

**Where:** a new `search_pattern_decision(payload, env)` in `src/jarvis/hooks.py`, beside
the other `*_decision` functions, returning the `_deny(reason)` shape from
`hooks.py:949-956`. Called from `preflight_decision`'s existing `mcp__` branch
(`hooks.py:1307-1309`), FIRST, before `investigator_write_decision` — that one returns
`None` for read-only Serena tools, so a refusal placed after it would still be reached,
but placing it first means an investigator gets the useful message rather than falling
through to nothing.

**The matcher must widen.** `src/jarvis/assets/settings.base.json:12` becomes

```json
"matcher": "Bash|Edit|Write|NotebookEdit|mcp__serena__search_for_pattern|mcp__plugin_serena_serena__search_for_pattern"
```

Both names, because a plugin install produces the long prefix and `claude mcp add serena`
the short one; Jarvis configures no MCP server so it cannot know which, and a matcher
naming a tool that does not exist is inert. This is exactly
`dispatch.SERENA_TOOL_PREFIXES`' rule (`src/jarvis/dispatch.py:45-48`), and the two names
must be built from that tuple in any test that asserts the matcher, not spelled a third
time.

Named exactly, not `mcp__.*`: the hook is a ~155ms process per matched call
(`dispatch.py:149-152`' measurement), Serena's symbol tools are called constantly, and
taxing every MCP call to guard one of them is the wrong trade. Propagation needs no
`TEMPLATE_VERSION` bump — `bootstrap.inject_settings` (`bootstrap.py:246-271`) rewrites an
unedited managed `settings.json` whenever the rendered text differs, and worker settings
are rebuilt from the asset at every dispatch.

**The predicate — what is refused.** Read `pattern = tool_input.get(
"substring_pattern")`, the tool's own parameter name, and
`crosses_newlines = tool_input.get("multiline", True)` (Serena defaults `multiline=True`,
which is `re.DOTALL | re.MULTILINE`). Deny on either of two shapes and nothing else:

1. **Two or more unbounded newline-crossing quantifiers, when newlines are crossed.**
   Count occurrences of `.*`, `.+`, `[\s\S]*`, `[\s\S]+`, `[\S\s]*`, `[\S\s]+`,
   `[\d\D]*`, `[\w\W]*` (and their `+` forms). Deny at two or more **only when
   `crosses_newlines` is true**. Catastrophic backtracking needs at least two quantifiers
   with overlapping languages; one `.*` over a file is not the bug.
2. **A nested unbounded quantifier** — a group that contains an unbounded quantifier and
   is itself quantified: `(...*...)*`, `(...+...)+`, `(.*\n)*`. Denied **regardless of
   `multiline`**: that is the classic exponential shape and it does not need DOTALL.

**What is explicitly allowed**, and the reason each matters — this hook sits in front of
every worker's search tool, and a predicate that over-refuses is worse than the hang it
prevents:

- A single `.*` anywhere, at any `multiline` (`def .*process`, `class .*Store`). This is
  the common case and it returns.
- Any number of `[^\n]*` — the very thing the denial recommends.
- Any number of `.*` with `multiline: false`, where `.` cannot cross a newline and the
  search is line-scoped.
- Bounded ranges: `.{0,200}`, `[\s\S]{0,500}`.
- Literal searches with no metacharacters at all.

Neither rule fires on any `search_for_pattern` call in this repository's ordinary usage.
The pattern from §1 fails rule 1 and rule 2 both.

**When it cannot parse the input:** `tool_input` missing, not a dict, or
`substring_pattern` absent or not a string — **return `None` (allow)**. The hook must
never refuse a payload it does not understand: the tool's schema is not Jarvis's to own,
and a renamed parameter would otherwise take every worker's search tool offline. The cost
of that choice is that a schema change silently disarms the hook, which is why §2's alarm
and §3's timeout exist behind it.

**The denial reason** names the fix, on `spec_shape_decision`'s pattern
(`hooks.py:928-935`) — a refusal that does not say what to type instead buys a reworded
retry of the same mistake:

> That pattern can backtrack catastrophically: `search_for_pattern` matches with DOTALL,
> so `.*` crosses newlines and two of them over a large file can hang the Serena server
> for minutes (issue #845 — 18m50s of CPU on one call). Bound the wildcards: use `[^\n]*`
> for a line-scoped match, a counted range like `.{0,200}`, or pass `multiline: false`.
> For a structural question use `find_symbol` / `get_symbols_overview` instead of a text
> search.

**Tests:** the §1 pattern is denied and the reason names `[^\n]*`; `def [^\n]*process`,
`class .*Store`, `TODO`, and `.*foo.*bar` with `multiline: false` are all allowed; `(.*\
n)*x` is denied at `multiline: false` too (rule 2); a payload with no `substring_pattern`
is allowed; the matcher in the rendered worker settings contains both Serena tool names,
built from `dispatch.SERENA_TOOL_PREFIXES`.

## 5. Couplings — checked, and what this does NOT touch

The knowledge base keeps a running list of what an alarm-adjacent change breaks. **No new
alarm KIND and no new alarm STATUS is introduced here** — `long-join` (`JOIN_ALARM`)
already exists and is already in `ALARM_KINDS`, and the new rows are `raised` like every
other non-informational alarm. So:

| Coupling | Touched? |
|---|---|
| `probes.RESERVED_IDS` duplicating `inspection.ALARM_KINDS` as literals, pinned by `tests/test_probes.py` and `tests/test_cache_ttl_alarm.py` | **No.** No kind added. Both pins keep holding with no edit. |
| `project_store.ALARM_STATUSES` pinned equal to `ops.ALARM_STANDING` by `tests/test_timeline.py` | **No.** No status added. |
| `cli.PART_LABELS` / `PART_SHORT` / `BAR_GLYPHS` pinned equal to `inspection.PARTS` | **No.** `PARTS` is unchanged; §1 moves seconds BETWEEN existing buckets. |
| `tests/test_inspection.py:805` `test_nothing_in_the_module_hard_codes_a_threshold` | **Yes**, but only to keep passing: the new threshold is a catalog setting read as `cfg.alarm_subagent_tool_minutes * 60`, so no literal is added and the `allowed` set needs no entry. Assert this rather than assume it. |
| `tests/test_inspection.py` `test_the_defaults_are_the_measured_ones` (:780-785) and the zero-refusal parametrisation | **Yes.** Both want the new `InspectConfig` key. |
| `bootstrap.TEMPLATE_VERSION` | **No.** Settings propagate by content diff (`bootstrap.py:255`); no OPERATION.md prose changes. |
| `ALARM_KINDS[JOIN_ALARM]` legend, "a join open past the cache TTL — the wait is paid for twice" | **Reworded.** It is now true of `TaskOutput` and of a hung delegation, not of every open delegation. |
| Committed transcript fixtures under `tests/data/transcripts/` | **Re-read.** Any fixture containing a foreground `Agent` span now reports different `blocked`/`tools` figures; a test asserting those figures moves and must be re-committed as the corrected value, not patched around. |

## 6. Non-goals

- **Nothing kills a process, and no remedy is added.** The `remedies` registry stays the
  closed two (`nudge`, `unblock`). The alarm reports; the `MCP_TOOL_TIMEOUT` in §4 is
  Claude Code's own bound, applied by the process itself.
- **Serena's regex engine is not fixed and not worked around further.** Rule 1's real root
  cause is that Serena matches with DOTALL and does not bound its own patterns — that is
  upstream and not this repository's. §4 is a symptom guard, deliberately, and it is
  stated as one: it refuses the shape that hangs rather than making the hang impossible.
- **`alarms()` still reads the LAST TURN of the main transcript only.** A hung subagent of
  an earlier turn is already paid for.
- **Subagent depth stays 1** (`SUBAGENT_DEPTH_READ`). A subagent of a subagent is counted
  and not read, here as in the report.
- **`investigator_write_decision`'s dead `mcp__` branch (§3 of the problem) is NOT fixed
  here.** Widening the matcher to every MCP tool is a different change with a different
  cost argument, and it is a control on a control — it wants its own order. File it.

## 7. Rejected alternatives

1. **Alarm on an open foreground `Agent` past `alarm_join_seconds`, as issue #845 literally
   asks.** Measured: 43 of 272 foreground delegations exceed 20 minutes, so at 300s this
   fires on roughly one delegation in six, nearly all of them healthy. An alarm that fires
   on normal work is trained away, and then it is worse than nothing — `alarms()`' own
   standing argument (`:1548-1550`). Neo, answering for the user, chose the hang-evidence
   gate instead.
2. **Leave `JOIN_TOOLS` alone and special-case `Agent` in the alarm loop.** Cheaper by two
   lines and it leaves §1c uncorrected: `jarvis inspect` would keep reporting "blocked on
   a subagent 0%" for every delegation the fleet makes, which is the half of this issue
   that is about the record rather than the alert.
3. **Raise `alarm_turn_minutes` above the hour, or lower it, so `long-turn` catches this.**
   Wrong finding, not a wrong threshold. `long-turn` says a turn is expensive; this says
   one call is wedged. Lowering it re-raises every long design turn in the fleet.
4. **Detect the hang from the MAIN transcript alone — a `tool_use` with no `tool_result`
   for N minutes, at any depth.** That is what `alarms()` can already see, and it is
   exactly what rule 2a does for `TaskOutput`. Applied to `Agent` it IS alternative 1: the
   lead's own transcript cannot distinguish "the subagent is thinking" from "the
   subagent's tool is hung". The evidence only exists in the subagent's file.
5. **Set `MCP_TOOL_TIMEOUT` in `settings.base.json` and stop there.** Two failures: a
   project could not raise it (the `env.update` in `dispatch` overwrites nothing it does
   not name, but the asset cannot see the catalog), and a timeout alone leaves the OS
   silent about the 19 minutes — the work order would have been cut off with no record of
   why.
6. **Refuse every `.*` in a `search_for_pattern`.** This hook is in front of every
   worker's primary search tool. `class .*Store` is the single most common legitimate
   pattern in this repository's own transcripts; refusing it would cost a retry on most
   searches the fleet makes, and a hook that fires on correct work is disabled by whoever
   owns the project. Two overlapping quantifiers, or a nested one, is the smallest
   predicate that separates the hang from normal use.
7. **Have `inspection` open the OS database to find the subagent transcripts.** Breaks
   `read_session`'s standing rule (`:1214-1226`) that this module walks only files Claude
   Code wrote, which is what keeps it callable from a test, a `--json` consumer and the
   daemon alike. The subagent files are already found on disk, beside the session file.
