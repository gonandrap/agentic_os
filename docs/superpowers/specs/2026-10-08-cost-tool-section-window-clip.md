# Clip `/cost`'s tool section to the selected window

Work order wo-c1a04671 · issue #990 · amends
`docs/superpowers/specs/2026-10-06-fleet-cost-per-tool.md` §10.4, §10.6, §10.7, §10.8,
§10.12 and extends
`docs/superpowers/specs/2026-10-07-cost-window-selector.md` §5–§6 to the third population
on the page.

## The problem

"What the tools cost" reports a population that is **not the window the page is about**.

Measured by the reporter on the live dashboard:

| surface | window headline | tool section |
|---|---|---|
| one order, `/cost` default window | **$6.50**, 42 in-window API calls | 385 tool calls, 24.4M carried tok, **$13.24 carried** |
| `--fleet --window 5h` | (same page) | 928 calls, **$27.93 carried** |

Carried dollars in a *subsection* exceed the whole window's spend by 2x and 4x. Every
other figure on the page describes `[since, until)`; this one describes entire sessions.

Root cause, three links, all confirmed by reading the code:

1. `fleetcost.report` (src/jarvis/fleetcost.py:1106) resolves the window to
   `start, end = picked["since"], picked["until"]` (line 1130), uses them for
   `turn_rows`, `window_facts`, `_neo_questions`, `_os_calls` and
   `compaction_payoff.gather` — and calls `tool_costs(orders.values(), cfg=cfg,
   floor=floor, index=index, orders_capped=capped)` at line 1195 **with no `start`/`end`
   at all**.
2. `tool_costs` (src/jarvis/fleetcost.py:974) therefore uses the window for ONE thing:
   which `order.session_id` values to look up in the session index. It then walks each
   matched transcript **end to end** — `_walk_tools(path, main, cfg, totals, by_tool)`
   (line 999) and the same call per `*/subagents/*.jsonl` (line 1005).
3. `_walk_tools` (src/jarvis/fleetcost.py:1048) folds in **every** entry
   `usage.tool_results(path)` returns, with no reference to `result.ts`; and
   `_Chain.carried` (src/jarvis/fleetcost.py:838) prices each result over every later
   call in the chain, bounded only by the first compaction after the result
   (`stop = next((c for c in self.compactions if c > ts), None)`).

So the window selects **which sessions get walked**, never **which calls get counted**,
and never bounds the carried ride. A single long-lived order dispatched weeks ago puts its
whole history into a 5-hour view.

This is the third instance of one defect class on one page. Knowledge entry
**`kn-0c297bdd`** (`jarvis learn show kn-0c297bdd`) recorded it when the other two
populations were clipped: *one page, several populations, one label*. The root cause named
there is the one here — a window that reaches the selection stage and stops. §10.4 of the
parent spec predates `2026-10-07-cost-window-selector.md` and states the carried-cost
model with **no notion of a window at all**; that omission is where the defect comes from,
and this spec amends §10.4 rather than contradicting it silently.

## The fix

Decided by Neo on question 1499, option **A**. Subsections 1–4 are that ruling; 5–9 are
the mechanics it implies.

### 1. A result counts only if its own `ts` is in `[since, until)`

`usage.ToolResult` carries two stamps: `call_ts` (when the `tool_use` block was written)
and `ts` (when the result landed, src/jarvis/usage.py:1246). **`result.ts` is the
selector**, half-open, matching every other window in the OS. A matched result outside the
window contributes nothing: no call, no error, no `result_tokens`, no carried tokens, no
carried dollars, no `token_basis` entry, no shape row, no `by_caller` entry.

Unmatched calls keep today's behaviour and are tested before the window test: an unmatched
call has no `ts` to place (it stayed `0.0`), and `excluded.unmatched_calls` is already its
disclosure. Order in `_walk_tools`: `matched` check first, window check second.

A **matched result whose `ts` is `0.0`** — `usage.parse_stamp` could not read the
timestamp — is outside every real window by the same arithmetic and is counted in the new
counter below with everything else the clip removed. No second rule and no second key: see
subsection 7 for why the window selector's separate `undated_messages` key is not copied
here.

### 2. The carried range is clipped to `until` as well

A result inside the window whose conversation continued past `until` must not be priced
over calls that are not in the window. The existing end bound stays; the new one is **in
addition**, whichever comes first:

```
end_of_ride = min( first compaction with ts > result.ts ,   # unchanged, §10.4
                   until )                                  # new
```

`since` does **not** bound the ride: a call later than the result is later than `since` by
construction. So `since` is a selector only, `until` is both a selector and a bound.

### 3. Where each bound lives, and why

**`since` and `until` reach `tool_costs` as REQUIRED keyword arguments.** Recorded as an
assumption on wo-c1a04671 and non-negotiable: an optional window defaulting to the whole
chain is exactly the silent default that produced this bug. A caller that forgets them
must fail at the call site, not report a wrong number.

```python
def tool_costs(orders: Iterable[OrderStats], *, cfg: CostConfig, floor: int | None,
               since: float, until: float,
               index: dict[str, list[Path]] | None = None,
               orders_capped: int = 0) -> dict[str, Any]
```

**`_walk_tools` takes both as required keyword arguments** and returns two counters:

```python
def _walk_tools(path: Path, chain: _Chain, cfg: CostConfig, totals: ToolCost,
                by_tool: dict[str, ToolCost], *, since: float,
                until: float) -> tuple[int, int]      # (unmatched, outside_window)
```

It owns result SELECTION because it is the only function that sees results. Return a plain
two-tuple rather than a dataclass: both call sites (lines 999 and 1005) accumulate into
local ints already, and a one-field-to-two-field change does not earn a type.

**`until` is stored ON `_Chain`, set by `_chain`, and NOT passed to `carried` per call.**
This is the one real design choice, and it goes this way for three reasons:

* **The chain must be BUILT over every call in the file, window or no window.**
  `_chain` (line 855) computes each call's rate via
  `compaction_payoff.prefix_rate(call, previous, i == 0)` and its cause via
  `usage.classify_boundaries` — both read a call against its PREDECESSOR. Dropping
  out-of-window calls before building would mis-price the first in-window call and lose
  the boundary at the left edge. This is the identical ruling the window selector already
  made for `_usage_of`: *boundaries are classified over ALL the file's calls and then
  filtered by `Boundary.ts`* (`2026-10-07-cost-window-selector.md` §5(c)). So the clip
  cannot happen at construction; it happens at read time.
* **The bound is a property of the chain's denominator, not of a result.** `carried(ts)`
  answers "which later calls in THIS chain carried it". `until` narrows the chain's call
  set; `ts` locates the result in it. Putting both in the argument list invites a future
  third call site to pass one and forget the other.
* **`carried`'s signature stays `carried(self, ts)`** — one caller today, and no call site
  can be written that silently skips the clip, because a `_Chain` cannot be constructed
  without an `until`.

```python
def _chain(calls: Sequence[usage.Call], compactions: Sequence[float],
           floor: int | None, *, until: float) -> _Chain        # required keyword

@dataclass
class _Chain:
    ...
    #: Exclusive right edge of the report's window. Calls at or after it are not in the
    #: window, so a result never rides on them. Required: see the spec's §3.
    until: float = math.inf     # see note below on the default
```

The dataclass field needs a default to sit after the existing defaulted fields. Use
`math.inf` and have `_chain` always pass the real value — `inf` is the identity for this
bound, so a hand-built `_Chain` in a test reads as "unclipped" rather than as "clipped to
1970". `_chain`'s keyword stays required, which is where the enforcement belongs.

Inside `carried`, after the existing compaction bound:

```
end = min(end, bisect.bisect_left(self.stamps, self.until))
```

`bisect_left` is correct for a half-open right edge: a call exactly at `until` is out.
The existing `if end <= start: return (0, zeros)` guard already handles a result whose
whole ride falls outside.

### 4. The excluded counter

`excluded` gains one key, **`outside_window`**: matched results the clip removed. The
block becomes

```
excluded: {unmatched_calls, no_transcript, orders_capped, sessions_walked, outside_window}
```

It counts RESULTS, not dollars: counting the carried dollars of excluded results would
require pricing them, which is the computation being removed, and would hand a reader a
figure that invites re-adding it to the total. The reader must be able to **see** that the
window hid something rather than infer it from a session count, which is the whole point
of the key.

`fleet.tools.version` stays **1** and `fleet.version` stays **1**: `excluded` gains a key
and no key is renamed, removed or re-typed, which is the additive rule §10.6 already
applies. The FIGURES change — that is the fix — and the shape does not.

### 5. Subagent chains get the same treatment. Explicitly.

`tool_costs` walks `path.with_suffix("") / "subagents" / *.jsonl` and builds a SEPARATE
`_chain(usage.calls_of(sub), usage.compaction_stamps(sub), floor)` per file (line 1005).
That chain needs `until=until` too, and `_walk_tools` on the subagent file needs
`since`/`until` too. A subagent transcript is where the big `sed_range` and `Read` dumps
live, so clipping only the main chain would leave most of the over-count in place and look
like the fix had failed. Both `_chain` call sites, both `_walk_tools` call sites.

### 6. Callers to update

Verified with `find_referencing_symbols`, not guessed. The full set:

1. **`fleetcost.report`**, src/jarvis/fleetcost.py:1195 — pass `since=start, until=end`.
   The only production caller of `tool_costs`.
2. **`tool_costs`** internals, src/jarvis/fleetcost.py:995 and :1005-1007 — the two
   `_chain` call sites gain `until=until`; the two `_walk_tools` call sites gain
   `since=since, until=until` and unpack the two-tuple.
3. **`_walk_tools`**, src/jarvis/fleetcost.py:1061 — `chain.carried(result.ts)` is
   unchanged; the clip is inside `carried`.
4. **`tests/test_fleetcost.py:805`**, the `tool_costs(sessions, *, floor=5_000,
   **cfg_keys)` helper — the only other caller. It gains `since=SINCE, until=UNTIL`
   defaults (the module constants at tests/test_fleetcost.py:32-33, which already bracket
   `T0`) and forwards them, so every existing direct-call case keeps its arrangement and
   its assertions. Test *helpers* may default; the production function may not.
5. **`tests/test_fleetcost.py:312`** `report()` already passes `since=SINCE, until=UNTIL`,
   so the report-level cases (`test_tools_payload_keys_stable`,
   `test_tools_respects_max_orders`, `test_an_order_with_no_transcript_is_counted_not_dropped`,
   `test_tools_read_only`) need only the `excluded` key-set literal at line 988 updated.

Prose to check, not code:

6. **src/jarvis/compaction_payoff.py:144** — the `per_token` docstring says
   "`fleetcost.tool_costs` prices a tool result's share of every later call it rode along
   in". That is now false as written: every later **in-window** call. One word; fix it
   where it stands rather than leaving a second answer in the tree.
7. **`tool_costs`'s own docstring** (line 977-983), which currently says
   "what every tool cost in the window" while doing nothing of the kind. State the two
   bounds and that the carried ride is clipped at `until`.

### 7. The two render surfaces

Both print `excluded`, so both must print the new counter. Silence when zero — nothing was
hidden, so there is nothing to disclose, the rule the window selector's `undated_messages`
footer already follows.

**CLI**, `_print_tool_cost`, src/jarvis/cli.py:3186. The last line today ends
`… · N calls with no result, excluded`. Append, only when non-zero:

```
 · M tool results outside the window, excluded
```

**Dashboard**, `src/jarvis/ui/templates/_fleet_distribution.html:94` block — the
`<p class="sub mono">` footer at lines 142-155 ends `… N transcripts read`. Append the
same sentence under the same condition, from the same payload key. No second computation.

This is also why `outside_window` is a count of results and not a second disclosure of
undated ones: the page gets ONE added sentence about what the window hid. Splitting it
into "outside the window" and "undatable" adds a line to two renderers for a population
nobody has observed in a real transcript, and an undated result genuinely is outside the
window.

### 8. Acceptance criterion

**Carried dollars in the tool section can never exceed the window's own spend.** Every
figure in a row now describes the same in-window population, so

```
fleet.tools.totals.carried_usd  <=  the window's total spend
```

holds by construction — a carried dollar is a SHARE of a call that is itself inside the
window. Assert it on a staged fixture (subsection 9, case 6). The reporter's $13.24
against $6.50 and $27.93 against the 5h headline are the two numbers this criterion
retires.

### 9. Unit tests the implementer writes

All in **`tests/test_fleetcost.py`**, appended to section 10 of that file (the
`# -- 10. §10.4/§10.6` block beginning at line 786). Not a new file: the fixtures
(`fleet_fixture`, `call_row`, `result_row`, `compact_row`, `transcript`,
`order_stats`, the `OPUS`/`READ_RATE`/`WRITE_RATE` constants) and the window constants are
all already there. Amends §10.8 of the parent spec, adding cases 13-18.

1. `test_a_result_before_since_is_excluded` — one transcript, a `Read` result at
   `SINCE - 60` with two later calls, a second `Read` result at `T0` with two later calls.
   Call the helper with the default window. Expect: `by_tool["Read"]["calls"] == 1`,
   `result_tokens` and `carried_*` from the `T0` result only,
   `excluded["outside_window"] == 1`, `excluded["unmatched_calls"] == 0`.
2. `test_a_result_after_until_is_excluded` — same arrangement mirrored: one result at
   `T0`, one at `UNTIL + 60` (and one call after it so it would otherwise carry). Same
   three assertions. Include a result at **exactly `UNTIL`** and assert it is EXCLUDED:
   the window is half-open and an off-by-one here is invisible in any other case.
3. `test_carried_range_is_truncated_at_until` — the case the fix exists for. A result at
   `UNTIL - 100` with four later calls: two before `UNTIL`, two after, no compaction
   anywhere. Expect `carried_calls == 2`, `carried_tokens == 2 * result_tokens`, and
   `carried_usd == pytest.approx(result_tokens * OPUS * (rate1 + rate2))` for the two
   in-window calls only — priced the way
   `test_carried_cost_splits_read_ttl_and_prefix` already prices, so the two post-`UNTIL`
   calls cannot hide inside a rounding tolerance. Add a second assertion with a
   compaction placed BEFORE `UNTIL`: the ride stops at the compaction, proving `min(...)`
   and not "whichever bound was added last".
4. `test_excluded_outside_window_counts_results` — two out-of-window results on one side
   and one on the other, plus one unmatched in-window call: `outside_window == 3` and
   `unmatched_calls == 1`, each counted once and in its own key. Also asserts the matched
   check runs before the window check (the unmatched call does not appear in
   `outside_window`).
5. `test_subagent_results_are_clipped_too` — extends the arrangement of
   `test_main_and_subagent_are_a_partition` (line 870): the `subagents=[[...]]` file gets
   a `Grep` result at `T0` with calls either side of `UNTIL`, and a second `Grep` result
   after `UNTIL`. Expect the late result excluded, the early one's `carried_calls` bounded
   by `until`, and `by_caller` still a partition of `totals`. This case is the guard
   against clipping only the main chain.
6. `test_tool_carried_usd_never_exceeds_window_spend` — the invariant, through
   `report()`. Stage one order whose session spans well past `UNTIL` (several large
   `call_row`s after it) with one in-window turn row carrying a known `cost_usd`. Assert
   `fleet["tools"]["totals"]["carried_usd"] <=` the window's spend from the same payload,
   and assert the same figure is strictly LESS than what the unclipped walk would give
   (construct the comparison by calling the helper with `until=` far in the future) — so
   the test fails if someone reverts the clip, instead of passing vacuously on a fixture
   where the two happen to agree.

Plus the existing-test edits named in subsection 6: the `excluded` key-set literal at
tests/test_fleetcost.py:988, and nothing else. If any other assertion in section 10 moves,
that is a signal the clip removed something it should not have.

### Rejected alternatives

* **(Neo's option B) Keep the whole-session carried figures and add a window-clipped set
  beside them.** Refused by Neo and refused here: two carried totals on one page is the
  defect `kn-0c297bdd` names, dressed as a feature. The reader who misread $13.24 as a
  window figure will misread whichever of the pair is printed larger. The page has one
  label and gets one population.
* **(Neo's option C) A flag or toggle for the unclipped view.** Refused: the window
  selector already IS that control. `--window` covering everything gives the full chain,
  which is Neo's explicit instruction. A second mechanism for the same question means two
  places to get the window wrong, and a `--json` consumer that must now ask which mode
  produced its numbers.
* **Select on `result.call_ts` instead of `result.ts`.** Rejected: the cost being measured
  is the result riding in later prefixes, and the ride starts when the result LANDS. A
  call issued inside the window whose result arrived after it produced no in-window
  carried cost, and the pair are seconds apart in every ordinary case — so this buys no
  robustness and loses the alignment with every other `[since, until)` in the OS.
* **Clip by dropping out-of-window calls before `_chain` builds.** Rejected on the window
  selector's own §5(c) ruling: `prefix_rate` and `classify_boundaries` read each call
  against its predecessor, so the first in-window call would be priced as a cold start and
  the left-edge boundary — usually the interesting one — would vanish. Build over
  everything, filter at read time.
* **Pass `until` to `carried()` per call instead of storing it on `_Chain`.** Rejected:
  argued in subsection 3. It makes the clip optional at every future call site, which is
  the shape of the bug being fixed.
* **Make `since`/`until` optional on `tool_costs`, defaulting to the whole chain.**
  Rejected, and recorded as an assumption on the work order. The default IS the bug.
* **Filter at `usage.tool_results`, giving it a window.** Rejected: `usage` is the leaf and
  its walk is defined by rows' positions relative to each other —
  `_size_group` measures a result against the call pair bracketing it, so a windowed walk
  would change `result_tokens` for the results at the edges. The sizing must see the whole
  file; `fleetcost` owns the window. Same division of labour as `cfg.chars_per_token`
  being a parameter rather than a catalog read.
* **Bump `fleet.tools.version` to 2.** Rejected: §10.6's additive rule. A key added to
  `excluded` is not a re-shaping, and a consumer that breaks on an added key was already
  broken.

### Deliberately NOT in scope

* **No change to §10.4's carried-cost arithmetic.** The rate model, the three money
  fields, `prefix_rate` reuse and the compaction bound are untouched. This spec adds one
  bound and one selector.
* **No reconciliation of `carried_usd` against the window's `cost_usd`.** §10.12's ruling
  stands: they are different decompositions of overlapping tokens. The invariant in
  subsection 8 is an INEQUALITY, deliberately — a share cannot exceed its whole — and must
  not be written as an equality in any test or note.
* **No audit of the other `/cost` sections.** `metrics`, `os_cost_by_kind` and
  `compaction_payoff` were clipped under `kn-0c297bdd` and are not re-examined here. If a
  fourth unclipped population exists, it is a finding, not this work order.
* **No `notes` entry about the clip.** `fleet.tools.notes` carries the six known
  INACCURACIES of §10.4's model; a correctly applied window is not an inaccuracy. The
  disclosure is the `outside_window` counter and the one sentence each renderer prints.
* **No caching, no new catalog setting.** §10.9 stands: `--fleet` is an on-demand report.
  The clip removes work; it adds none.
