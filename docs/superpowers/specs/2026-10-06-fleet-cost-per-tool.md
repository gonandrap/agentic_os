# Fleet cost per tool — §10 of the fleet-cost build order

Work order wo-38456776. **Addendum to
`docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md`** (that document is §1-§9;
this is §10). Nothing in §1-§9 changes. Every rule that document sets — `fleetcost.py` as
the one new module, `usage.py` as a leaf that must not import a store, `version: 1` under
the `fleet` key, dollars as a FLOOR at list prices (kn-e6bb1166), catalog settings instead
of module constants, fixtures in `jarvis/testing.py` — binds this section too and is not
restated.

Deterministic, read-only, no model call.

## The problem

**The fleet's largest avoidable token line is tool output, and no surface in the OS can
see it.** §1-§9 report cost per ORDER and per TURN. A turn's cost is dominated by the
conversation it carries, and the conversation is mostly tool results — but the payload
§4 defines has no tool in it at all, so the fleet's dearest habit is invisible to the
report built to find expensive habits.

Four concrete gaps, with evidence:

1. **No tool dimension anywhere in the cost payload.** `usage.Usage`
   (src/jarvis/usage.py:203-380) has twenty-three fields and not one names a tool.
   `_usage_of` (usage.py:896-942) walks `_assistant_messages` only, and
   `_assistant_messages` pre-filters the file on the needle `'"usage"'` (usage.py:601) —
   `tool_result` blocks live on `user` rows and carry no `usage` object, so the module
   that owns token accounting **never reads a tool result at all**. §4's `metrics` dict
   inherits that blindness.
2. **The measurement exists, outside the product.** An ad-hoc walk over 769 worker
   transcripts found ~23M tokens of tool output, **55% of it file dumps** — `sed -n`
   ranges 40%, `Read` 13%, `cat` 3% — while `pytest` and clipped streams averaged ~167
   tokens/call. The same shape was measured independently at 276 transcripts and written
   down in `docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md:17`
   (`sed -n` inside Bash: 3,902 calls) and :388 (Bash-navigation over `.py` = **41.3% of
   conversation-body read volume**). Both are one-off scripts. Neither is reachable from
   `jarvis cost`, so neither can be re-run to show whether a fix worked.
3. **Fixes to this are already being shipped with no instrument to judge them.** The
   steer spec's own §5.3 (:371-391) states plainly that "AFTER cannot be measured inside
   this work order" and defers it to a follow-up work order. `py_nav_hook` has the same
   problem: it exists to replace `sed -n` file dumps with symbol calls, and there is
   nothing in the OS that would show the 40% share falling. **This section is that
   follow-up**, made a product surface instead of a script, so the after-figure is a
   command the user runs rather than a measurement someone has to re-derive.
4. **Three readers already parse `tool_use`/`tool_result` independently, and a fourth is
   the defect pattern.** `inspection.read_transcript` (inspection.py:1185-1212),
   `live.py:496-505` and `background._scan_calls`/`_scan_results` (background.py:158-201)
   each walk the same two block types with their own matching logic. `usage.rows`'
   docstring states the rule this violates (usage.py:526-531): "every reader in the OS
   wants a different projection of it … they share this so a change in how Claude Code
   writes a file lands in one place." The tool WALK was never lifted, only the line
   reader.

**Root cause of 1-3:** the token ledger is keyed by API CALL (`usage.Call`, usage.py:383),
and a tool result is not a call — it is a payload that rides along inside every
*subsequent* call's prefix. Nothing in the accounting has a place to charge a cost that
belongs to no call but is paid by many. That is the quantity named `carried cost` below,
and it is why counting result bytes (what both ad-hoc scripts did) understates the
problem: a 20k-token `sed -n` dump read 30 more times costs 600k token-reads, not 20k.

**Root cause of 4** is a missing leaf function, fixed here by adding one rather than by a
fourth copy.

## The fix

One additive key, `fleet.tools`, computed by one new leaf walk plus one pure classifier.

### 10.1 Where the code lives

Split in two, against §1's layering rather than beside it.

**The walk goes in `src/jarvis/usage.py`:** `tool_results(path) -> list[ToolResult]`,
plus the `ToolResult` dataclass. Arguments:

* It is transcript PARSING and nothing else — no store, no catalog, no prices. That is
  exactly `usage.py`'s contract as §1 states it ("a leaf that knows transcripts and
  prices and must never import a store"), and the new function imports nothing new.
* `jarvis inspect` wants the same walk. `inspection.py` already profiles tools
  (inspection.py:16, `ToolSpan`:466-515) and imports `usage` as `usage_mod`. If the walk
  lived in `fleetcost.py`, `inspection` would have to import a module that reads stores
  and the catalog — a leaf depending on a layer above it. Putting it in `usage.py` keeps
  the dependency arrow pointing the one way it points now.
* It retires defect 4 above: `inspection`, `live` and `background` can each migrate onto
  it later (**not in this work order** — see 10.10).

**The aggregation goes in `src/jarvis/fleetcost.py`:** `classify_command`,
`tool_costs(...)` and the rollups. It needs the catalog (`CostConfig`), the window, the
`max_orders` cap and `work_orders.session_id` — all of which `fleetcost` already holds per
§2, and none of which `usage.py` may touch.

**Not a third module.** §1 argues `fleetcost.py` into existence on one constraint
(`mode=ro`, never construct `ProjectStore`); this section adds no second constraint, so it
adds no second module.

### 10.2 Finding a tool_use/tool_result pair, and sizing the result

**Pairing.** Exactly the join the three existing readers use, now in one place:

* An `assistant` row's `tool_use` block carries `id` and `name`, and `input` is the
  tool's arguments (inspection.py:1185-1191; for Bash the command is `input.command`,
  background.py:197).
* The matching result is a `tool_result` block on a later `user` row whose
  `tool_use_id` equals that `id` (inspection.py:1206-1207, live.py:504-505,
  background.py:192-193). Blocks are reached with `usage.blocks_of(row, kind)`
  (usage.py:550-561), which already tolerates `message.content` being a bare string.
* Error results are counted but kept apart: reuse `background._failed`'s precedence —
  `is_error` on the block, then `is_error`/`isError` on the sibling `toolUseResult`, then
  the `ERROR_PREFIX = "Error: "` text fallback (background.py:58-62, 204-216). A refused
  call returns a two-line refusal and must not be averaged in with a 20k-token dump.
* An unmatched `tool_use` (turn killed mid-call, the case `ToolSpan.ended == 0.0` already
  models at inspection.py:470-473) is counted in `excluded.unmatched_calls` and
  contributes **no** tokens and **no** carried cost. Never zero-filled.
* Row filtering: `usage.rows(path)` **with no needle**. The needle `'"tool_'` would catch
  both block types, but the context-delta estimator below also needs `assistant` usage
  rows, prompt rows and `compact_boundary` rows from the same ordered walk, and one
  unfiltered pass beats four filtered ones. Precedent: `inspection.read_transcript`
  already walks unfiltered (inspection.py:1155) for exactly this reason — the things it
  collects are defined by their position relative to each other.

**Sizing. The transcript carries NO per-result token count.** Verified: token counts in a
transcript exist only in `message.usage` on `assistant` rows — that is the sole source
`_assistant_messages` reads (usage.py:596-631) and the sole shape `Call` models
(usage.py:400-408). A `tool_result` block carries `content` (a string, or a list of text
blocks — `background._text_of`, background.py:268-274) and a sibling `toolUseResult`
object, and neither has a `usage`. So the size is **derived**, two ways, and which one was
used is reported per tool:

1. **`context-delta` (primary, token-exact when it applies).** `Call.context` is
   `input + cache_write + cache_read` (usage.py:409-416) — the full prompt size of one
   API call, independent of how those tokens were priced. For two consecutive calls
   `N`, `N+1` in the same chain:

   ```
   appended_tokens = context(N+1) - context(N) - output(N)
   ```

   `appended_tokens` is split across the tool results that lie between them **in
   proportion to their character counts** (parallel tool calls put several results in one
   `user` row). Applies ONLY when all of these hold, else fall through to 2:
   * no prompt row between `N` and `N+1` (a `user` row that `inspection._prompt_of`
     would return a `Prompt` for, inspection.py:1090-1110) — otherwise the user's text is
     charged to the tool;
   * no `compact_boundary` row between them (`usage.compactions_in`, usage.py:843-872) —
     the context goes DOWN there;
   * `appended_tokens > 0`;
   * `N+1` exists (a result after the last call of the session has no delta).

2. **`chars` (fallback).** `ceil(len(text) / cost.chars_per_token)` where `text` is
   `background._text_of(block["content"])` and `cost.chars_per_token` is a catalog
   setting (default **4.0**, 10.7). A catalog number, not a literal, because it is a
   model-family property the OS does not control.

Unit of both: **`tokens`**, with `token_basis: {context_delta: n, chars: n}` beside every
figure so a reader can see how much of a number is measured and how much estimated. There
is no third option and no invented constant: the estimator's divisor is configuration.

### 10.3 Main agent vs subagent

**Reuse `read_session`'s existing attribution; invent nothing.** Claude Code writes each
subagent's transcript in its own file under `<segment>/subagents/*.jsonl`, and
`usage.read_session` already splits a session on exactly that — main segments into
`SessionUsage.main`, each subagents file into `SessionUsage.subagents` /
`each_subagent` (usage.py:1037-1049), named by `_subagent_detail` from the sibling
`.meta.json` (usage.py:500-518).

So the caller of a tool call is **the file its transcript row came from**:

| caller | transcript path |
|---|---|
| `main` | a path in `usage.index_sessions()[session_id]` (usage.py:997-1019) |
| `subagent` | a path under `<that path with suffix stripped>/subagents/*.jsonl` |

Two things follow and must be stated in the payload:

* `fleet.tools.by_tool[t].by_caller.subagent` is keyed only by the two words, not by
  agent type. `Subagent.label` (usage.py:452-455) could give a per-agent-type breakdown;
  deliberately out (10.10) — it multiplies the table by the number of agent types and the
  requirement asks for two columns.
* A subagent's tool result rides on the SUBAGENT's later calls, never the lead's. The
  chains are separate conversations — `usage.session_calls` is documented as "the LEAD
  agent's API calls … a subagent writes its own transcript" (usage.py:796-804). Carried
  cost for a subagent call therefore walks `usage.calls_of(sub_path)` (usage.py:699-719),
  not `session_calls`.

### 10.4 The carried-cost model

**Base, verbatim from the requirement:** result size x the number of later API calls in
that session it rides along in. Priced:

```
carried_usd(result) = result_tokens
                    * Σ over later calls c in the SAME CHAIN, ts(c) > ts(result),
                        before the next compaction in that chain:
                          per_token(c.model) * rate(c)

rate(c), kind(c) = compaction_payoff.prefix_rate(c, previous_call, is_first_call)
per_token(m)     = usage.price_for(m)[0] / 1e6
```

* **`prefix_rate` is reused, not reimplemented** (scripts/compaction_payoff.py:137-145,
  moving to `src/jarvis/compaction_payoff.py` in §1's Stage 1). It returns the multiple of
  base input price that one call paid *for the prefix it carried*, which is precisely the
  rate this result was billed at inside that call: `RATE_READ` = `usage.CACHE_READ_RATE`
  (0.10), `RATE_WRITE` = `usage.write_rate(...)` (1.25x at the 5-minute TTL, up to 2.0x
  as the 1-hour share rises, usage.py:139-152), `RATE_INPUT` = 1.0. `_per_token`
  (compaction_payoff.py:147-149) is reused as well and becomes public **`per_token`**,
  for `quantile`'s reason in §1: `fleetcost` is its second caller.
* **The rate constants fit and are reused.** `RATE_WRITE`/`RATE_READ`/`RATE_INPUT`
  (compaction_payoff.py:97) are the three kinds; the numeric rates belong to `usage`
  (`CACHE_READ_RATE`, `CACHE_WRITE_RATE`, `CACHE_WRITE_1H_RATE`, usage.py:106-108) and are
  reached only through `prefix_rate`. No new rate constant is defined here.
* **"Later API calls in that session" counts, exactly:** calls from the SAME TRANSCRIPT
  CHAIN only. Main chain = `usage.session_calls(session_id)`, which concatenates every
  segment of the session (usage.py:796-811) — a result does keep riding across a segment
  boundary, because the conversation did. Sidechain = `usage.calls_of(sub_path)` for the
  one subagent file. A lead call is **never** in a subagent's denominator, nor the
  reverse. The window ends at the first compaction with `ts > ts(result)`
  (`usage.compaction_stamps`, usage.py:875-877): a compaction replaces the conversation,
  so the result stops being carried.
* **Cache read vs TTL-expiry rewrite, split and reported separately.** `prefix_rate`'s
  `kind` gives read-vs-write; the *cause* of a write comes from
  `usage.classify_boundaries(calls, compactions=…, cold_prefix_floor=…)`
  (usage.py:742-793), joined to the call by `Boundary.ts`. Three money fields, never
  summed in the render without their names:
  * `carried_read_usd` — `RATE_READ` calls.
  * `carried_rewrite_ttl_usd` — write calls whose boundary cause is `BOUNDARY_TTL`.
  * `carried_rewrite_prefix_usd` — cause `BOUNDARY_PREFIX`, `BOUNDARY_COMPACTED`,
    `BOUNDARY_UNDECIDED`, or a write call with no boundary at that ts.
  `cold_prefix_floor` comes from the catalog with no fallback, exactly as §2's
  `boundary_counts` takes it (`bill._cold_prefix_floor`, src/jarvis/bill.py:1144). With no
  floor the TTL split is `BOUNDARY_UNDECIDED` and lands in neither TTL bucket — the rule
  `usage.fold_boundaries` already enforces (usage.py:945-965), and the renderer must not
  print it as $0.00 TTL.
* **`carried_tokens`** = `result_tokens * len(later_calls)` — the raw, price-free figure.
  Tokens are the primary unit everywhere in this module (usage.py:62-66); dollars are
  derived.

**Dollars are LIST PRICES and a FLOOR, never a bill.** `fleet.tools` inherits §4's
`floor: true` / `floor_reason: ops.COST_FLOOR_NOTE` and adds nothing of its own.

#### Known inaccuracies

Stated, not hidden. Each rides in `fleet.tools.notes`.

1. **`result_tokens` is derived, never reported by the API.** `context-delta` is exact to
   the token for a single result between two clean calls; with K parallel results it is a
   char-proportional split of an exact total; `chars` is an estimate whose divisor is
   configuration. The counts are in `token_basis`.
2. **`output(N)` is a generation count, not an input count.** The assistant message is
   re-sent as input on call N+1, and the two tokenizations need not agree (thinking blocks
   may be retained or stripped). Any difference lands on the tool result in
   `context-delta`. Direction is unknown, magnitude is small relative to a file dump, and
   it is why `chars` is kept as a visible second basis rather than deleted.
3. **Carried cost assumes the result survives in the prefix until the next compaction.**
   Context-window truncation and any harness-side micro-compaction the transcript does not
   record would end the ride earlier, so carried cost is an **over**-estimate in those
   sessions. `compact_boundary` is the only replacement event the transcript carries
   (usage.py:851-853 states this explicitly: the compaction's own API call writes no
   assistant message).
4. **The pricing of a result's SHARE of a call is exact; its size is not.** One call pays
   one rate for its whole prefix (`prefix_rate` returns one multiple), so
   `share = result_tokens * rate * per_token` introduces no blending error. Error enters
   only through 1 and 2.
5. **A result that was never carried still cost its own round trip.** The last tool call
   of a session has `carried_calls: 0` and `carried_usd: 0.0`; its `result_tokens` are
   still reported. The zero is honest and not a defect.
6. **`cost.chars_per_token` is uncalibrated against the fleet.** No work order in this
   build order measures it. Default 4.0, overridable, and the `token_basis` counts let a
   reader see how many rows depend on it.

### 10.5 The Bash command-shape classifier

```python
def classify_command(command: str) -> str
```

Pure, total, deterministic. **Never shells out, never touches the filesystem** — no
`os.path.exists`, no glob expansion, because the path may be in a worktree that is gone
(the reason `usage.index_sessions` exists, usage.py:1000-1004) and a classifier whose
answer depends on the disk is not reproducible.

**Tokenizing.** `shlex.split(command, comments=False, posix=True)`; on `ValueError`
(unbalanced quote) fall back to `command.split()`. Segments are cut on the shell operators
`|`, `||`, `&&`, `;` and `|&` found as standalone tokens. The **head word** of a segment
is its first token with any leading `VAR=value` assignments and a leading `sudo`,
`time`, `env`, `nohup`, `xargs`, `uv run`, `poetry run` wrapper stripped.

**The ordered rule list. First match wins; nine shapes, the last total.**

| # | shape | rule |
|---|---|---|
| 1 | `pytest` | any segment's head word is `pytest`, or head word in `{python, python3, uv, poetry}` and `pytest` is a token of that segment |
| 2 | `jarvis` | any segment's head word is `jarvis` |
| 3 | `git_read` | first segment head word is `git` **and** its next token is in `{diff, show, log, blame}` |
| 4 | `sed_range` | first segment head word is `sed`, `-n` is a token, and some token matches `^['"]?\d+(,(\d+\|\$))?p?['"]?$` |
| 5 | `cat_file` | first segment head word is in `{cat, bat}` |
| 6 | `search` | first segment head word is in `{grep, egrep, fgrep, rg, ag, find}` |
| 7 | `stream_head_tail` | the LAST segment's head word is in `{head, tail}` (covers both `cmd \| head` and a bare `head -50 file`) |
| 8 | `other` | everything else |

**Tie-breaks, explicit.** The axis is **what produced the bytes**, not what clipped them,
so a producer rule (3-6) is tested before the clipping rule (7):

* `sed -n '1,50p' f | head -20` → **`sed_range`**, not `stream_head_tail`.
* `cat f | head` → **`cat_file`**.
* `git log | head -5` → **`git_read`**.
* `jarvis cost --json | head -40` → **`jarvis`** (rule 2 scans every segment, so a
  jarvis call is never hidden behind a pipe; OS self-calls are a thing the user asks
  about by name).
* `uv run pytest -q 2>&1 | tail -40` → **`pytest`** (rule 1 before all).
* `./build.sh | head` → **`stream_head_tail`**. That is what rule 7 is FOR: the residual
  "output already clipped" bucket, which is the ~167 tok/call population the motivating
  measurement found was already fine.

`git status`, `git commit`, `ls`, `sed -i`, `sed -n 'p'` (no range) and a `sudo`-wrapped
anything-else are all **`other`** by construction. Shapes partition Bash calls exactly
once: `sum(shapes[*].calls) == by_tool["Bash"].calls`, asserted by a test (10.8.5).

**Non-Bash tools are keyed by `tool_use.name` verbatim** — `Read`, `Edit`,
`mcp__plugin_serena_serena__find_symbol`, and so on. No normalization and no prefix
stripping in the payload: two spellings of one tool is how a reader loses a row. The
render truncates for display only (10.6).

### 10.6 `--json` shape

Additive under the existing `fleet` key. `fleet.version` stays **1** — §4's version
governs the payload as a whole and an added key is additive, which is the rule §4 already
applies to `cost_report`. `fleet.tools` carries its OWN `version: 1` so a later
re-shaping of this subtree is detectable without bumping the parent.

```
fleet: {
  ... §4 keys unchanged ...
  tools: {
    version: 1,
    totals:  <ToolCost>,                       // every tool, both callers
    by_tool: { "<tool_use.name>": <ToolCost> },
    excluded: {unmatched_calls, no_transcript, orders_capped, sessions_walked},
    notes: [ "<the six known-inaccuracy sentences>" ]
  }
}
```

`<ToolCost>`, one shape used at every level:

```
{
  calls:                     int,    // count
  errors:                    int,    // count; subset of `calls`
  result_tokens:             int,    // tokens, summed
  result_tokens_avg:         float,  // tokens per call
  result_tokens_p90:         int,    // tokens, nearest-rank via compaction_payoff.quantile
  result_tokens_max:         int,    // tokens
  carried_calls:             int,    // count: later API calls summed over results
  carried_tokens:            int,    // tokens: Σ result_tokens × later calls
  carried_usd:               float,  // usd (list, floor)
  carried_read_usd:          float,  // usd
  carried_rewrite_ttl_usd:   float,  // usd
  carried_rewrite_prefix_usd:float,  // usd
  token_basis: {context_delta: int, chars: int},   // counts of calls, per estimator
  share_of_result_tokens:    float,  // share, 0..1, of fleet.tools.totals.result_tokens
  share_of_carried_usd:      float,  // share, 0..1
  by_caller: { main: <ToolCost>, subagent: <ToolCost> },   // absent inside by_caller
  shapes:    { "<shape>": <ToolCost> }                     // Bash only; absent elsewhere
}
```

* `by_caller` is a **partition**: `main.calls + subagent.calls == calls`, same for every
  additive field. kn-7a2180ba's rule — make the finer accounting a partition of the
  coarser, never an addend — which `usage.Usage.rewrite_ttl_write` cites at usage.py:221-226.
* `by_caller` and `shapes` are **omitted inside** a nested `<ToolCost>`, so the structure
  is two levels deep and cannot recurse.
* `p90` uses `compaction_payoff.quantile` at `cfg.percentile`, nearest-rank, for §2's
  reason: the figure is always a result that actually came back.
* `by_tool` is a **dict keyed by tool name**, matching §4's `metrics` dict and for the same
  stated reason: a dashboard wants `tools.by_tool["Bash"].shapes["sed_range"]` without a
  scan.
* `excluded.sessions_walked` is how many transcripts were read; `orders_capped` is how
  many orders the `max_orders` cap dropped (10.9). Both are in the payload so a share can
  be checked against a denominator.

### 10.7 The text render

One compact table in `jarvis cost --fleet`, printed **after** `os_cost_by_kind` and before
the compaction-payoff block, under the heading `tool cost`:

```
tool                             calls   result tok   carried tok   carried $   main/sub
Bash · sed_range                  3902        9.2M         61.4M      142.07   91% / 9%
Read                              1874        3.1M         19.8M       45.12   63% / 37%
Bash · cat_file                    611        0.7M          4.1M        9.44  100% / 0%
mcp__…serena__find_symbol          412        0.2M          1.1M        2.51   88% / 12%
…
(14 more tools, 0.4M result tok, $3.10 carried)
```

* **Sort order:** `carried_usd` descending, tie-broken by `result_tokens` descending, then
  by tool name ascending. Total order, so the table is reproducible run to run. Sorting on
  carried cost rather than calls is the whole point of 10.4 — `pytest` at 167 tok/call
  must not outrank a `sed -n` dump because it was called more often.
* **Rows:** Bash is rendered as one row per shape (`Bash · <shape>`) and NOT as a Bash
  total line — the total is recoverable, and two rows that sum to a third invites the
  reader to add the wrong pair. Every other tool is one row.
* **Truncation:** at most `cost.tool_rows` rows (catalog, default 20), then one summary
  line `(N more tools, X result tok, $Y carried)`. Nothing is silently dropped. A tool
  NAME longer than 28 characters is middle-elided with `…`; the `--json` payload always
  carries it in full.
* `main/sub` is the carried-dollar split as whole percents. When
  `cold_prefix_floor` left boundaries undecided, the TTL column is suppressed with the
  word `undecided` rather than `$0.00` (usage.py:328-337's standing rule for renderers).

Dashboard: `fleet.tools` renders into the §5 partial
`ui/templates/_fleet_distribution.html` as a second table. No second computation, same
payload.

### 10.8 Unit tests

Appended to `tests/test_fleetcost.py` (§7), plus the walk's own tests in a new
`tests/test_usage_tool_results.py`. All synthetic. Fixtures go in **`jarvis/testing.py`**,
never `conftest.py`, per `mem:testing`; extend §7's `fleet_fixture` with a
`tool_transcript(...)` builder that writes a JSONL with chosen `assistant`/`user` rows,
`tool_use`/`tool_result` ids, `usage` blocks and an optional `subagents/` directory.

1. `test_tool_results_pairs_by_id` — two `tool_use` blocks, results arriving out of
   order and one in a multi-block `user` row: both pair; a `tool_result` with an unknown
   `tool_use_id` is ignored; a `tool_use` with no result lands in
   `excluded.unmatched_calls` with no tokens.
2. `test_result_tokens_context_delta_exact` — one result between two calls with
   hand-chosen `context`/`output`: `result_tokens` equals the delta exactly,
   `token_basis.context_delta == 1`, `chars == 0`.
3. `test_result_tokens_falls_back_to_chars` — the same file with a prompt row between the
   calls, and again with a `compact_boundary` between them, and again with a negative
   delta: all three report the `chars` basis and the counts say so.
4. `test_parallel_results_split_by_chars` — three results between one call pair, char
   lengths 1:1:2: the exact delta is split 25/25/50 and the parts **sum to the delta**.
5. `test_classify_command_table` — one parametrized case per shape plus the six tie-break
   strings in 10.5 verbatim, plus two adversarial ones: `sed -n` inside a quoted argument
   that must NOT classify as `sed_range` (`git commit -m "use sed -n 1,5p"` → `other`),
   and an unbalanced quote (`cat 'f.py | head` → `cat_file`, via the whitespace fallback,
   and no exception). Also asserts the shapes partition Bash calls exactly.
6. `test_main_and_subagent_partition` — a transcript with a `subagents/` file: the two
   callers' calls and tokens sum to the parent figure, and the subagent's carried cost is
   computed over the subagent's OWN calls only (a lead call after the subagent's last one
   does not raise it).
7. `test_carried_cost_stops_at_compaction` — a result with five later calls, a
   `compact_boundary` after the second: `carried_calls == 2`.
8. `test_carried_cost_splits_read_and_ttl` — a later call sequence with one `BOUNDARY_TTL`
   and one `BOUNDARY_PREFIX`: the three money fields are distinct, sum to `carried_usd`,
   and the TTL one is zero in neither direction by accident. With
   `cold_prefix_floor=None` the TTL field is `None`, never `0.0`.
9. `test_error_results_counted_not_averaged` — a refused Bash call (`is_error`) and a
   `"Error: …"` text result: `errors == 2`, and `result_tokens_avg` of the tool is
   unchanged by them beyond their own small size.
10. `test_tools_payload_keys_stable` — the `fleet.tools` key set and one `<ToolCost>`'s
    key set match literals in the test, the §7.9 guard against a renderer-driven rename.
11. `test_tools_respects_max_orders` — `cost.max_orders = 1` over three orders:
    `excluded.orders_capped == 2`, `sessions_walked == 1`.
12. `test_tools_read_only` — §7.10's assertion extended: the transcript files' mtimes are
    unchanged and `ProjectStore` is never constructed.

### 10.9 Cost control

**The bound.** One unfiltered pass per transcript, O(bytes). The walk is linear and holds
only `{tool_use_id: pending call}` plus the per-tool accumulators — not the file. Two
passes per session in total: `usage.rows(path)` for the tool walk, and the call list from
`usage.session_calls` / `calls_of` for the carried arithmetic (`calls_of` re-reads with
the `'"usage"'` needle, which skips ~75% of rows, usage.py:531). Reference size for the
whole fleet: **769 transcripts, ~23M result tokens** — the measurement in "the problem".

**The cap is `cost.max_orders` from §6**, unchanged and not duplicated: the orders
`fleetcost` already selected for the window are the sessions walked. Transcripts are
walked only for orders inside the window, so a narrow `--since`/`--until` is the user's
lever. `excluded.orders_capped` and `excluded.sessions_walked` publish what the cap did.

**No cache in this stage.** `--fleet` is an on-demand report, not a 15s pulse — the
argument `/cost` already makes at src/jarvis/ui/app.py:1389-1392 and §5 inherits. If a
full-fleet `--fleet` on production state exceeds **10 seconds** wall clock, add a cache
then, with this key and nothing cleverer: `$JARVIS_HOME/cache/toolcost/<session_id>.json`
guarded by `(path, st_mtime_ns, st_size)` of every segment, written atomically, and
ignored on any mismatch. Specified here so the follow-up has no design left to do; **not
built in this work order.**

### 10.10 Catalog settings

Two keys added to §6's `CostConfig`, parsed by the same `_parse_cost` with the same
field-level inheritance, covered by the same `("*.cost.*", "hot")` `APPLY_RULES` entry.
No module constant for either.

```python
chars_per_token: float = 4.0   # refused unless > 0
tool_rows: int = 20            # refused unless >= 1
```

### 10.11 Rejected alternatives

* **Count result BYTES only, as both ad-hoc scripts did.** Rejected: it is the symptom.
  A 20k-token dump read 30 more times is a 600k-token charge, and ranking tools by result
  size puts a big one-off `git diff` above a small dump in a long session. Carried cost is
  the quantity the fix (`py_nav_hook`) is meant to move.
* **Add tool fields to `usage.Usage`.** Rejected: `Usage.__add__` (usage.py:260-294) is a
  mergeable total for a session, and a per-tool dict of dicts merged there would be
  carried by every cost surface in the OS to serve one report. §4's rule — the fleet
  payload ADDS a section rather than re-shaping an existing contract — applies one level
  down too.
* **Put the whole thing in `inspection.py`, which already has `ToolSpan`.** Rejected:
  `inspection` answers "where did the TIME go" for ONE order and says so
  (inspection.py:16); it has no window, no cap, no catalog `CostConfig` and no fleet
  denominator. It should CONSUME `usage.tool_results` later (below), not own the rollup.
* **Migrate `inspection`, `live` and `background` onto `usage.tool_results` now.**
  Rejected for THIS work order, and it is the defect-4 root cause left standing on
  purpose: three live readers with their own matching logic, one of them in the reaper
  path (`background.orphaned_in_turn`, background.py:99-121, documented as something that
  must never raise). Three behaviour-preserving refactors beside a new feature is how both
  land unreviewed. File a follow-up. The new leaf makes it a deletion rather than a
  rewrite.
* **Estimate result tokens with a real tokenizer (`tiktoken` or an API count call).**
  Rejected twice over: a model call breaks the "mechanical, deterministic, read-only"
  contract this whole build order is built on, and a vendored tokenizer is a new
  dependency whose answer for Claude is an approximation anyway. `context-delta` is
  *exact* where it applies, which no tokenizer would be.
* **Per-agent-type subagent breakdown** (`Subagent.label`, usage.py:452-455). Rejected:
  multiplies the table by the number of agent types for a question nobody asked. The
  requirement names two callers.
* **A `PostToolUse` hook that measures output as it happens.** Rejected: the transcript
  already has the data, a hook cannot see the LATER calls a result rides on (the whole
  carried-cost model), and the adjacent spec already cut a flooding guard at that layer
  (`docs/superpowers/specs/2026-09-26-bounded-model-inputs.md:339`).
* **Bump `fleet.version` to 2.** Rejected: §4 declares the key additive and
  `cost_report`'s own keys untouched; an added subtree with its own version is the pattern
  already in use.

### 10.12 Deliberately NOT in scope

* **No alarm, no inbox item, no threshold on a tool's share.** §9's rule stands: this is a
  report someone runs. `jarvis inspect`'s alarms own "raise it while it burns".
* **No before/after figure for `py_nav_hook` in this work order.** The instrument ships
  here; the after-figure needs fleet time, exactly as the steer spec states at
  `2026-10-01-the-steer-that-beat-the-brief.md:371-373`. Quoting one from this worktree
  would be quoting the fixture.
* **No change to any tool, hook or steer.** This section measures; it does not intervene.
* **No reconciliation of `carried_usd` against `wo_turns.cost_usd`.** They are different
  decompositions of overlapping tokens and summing them would double-count. `carried_usd`
  is a SHARE of money already counted in §4's `cost_per_order_usd`, and the payload says
  so in `notes`.
* **No per-turn tool attribution.** `fleet.tools` is per window, per tool, per caller.
  Charging a tool call to a turn needs the turn-binding `inspection._bind_turns` already
  owns, and the §9 note about `agent_calls` having no turn id applies here in the same
  shape.
