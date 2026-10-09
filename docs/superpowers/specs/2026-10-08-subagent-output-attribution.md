# Subagent output attribution: the lead stops absorbing what no transcript measured

wo-132da4d4 · issue #988 · decided by the user via Neo question 1500

## The problem

Two defects, one cause.

**Cause.** A background subagent transcript
(`~/.claude/projects/<slug>/<session>/subagents/agent-*.jsonl`) stores a large share of
its assistant messages as mid-stream snapshots only: `message.stop_reason` is null,
`message.usage.output_tokens` is a streaming placeholder (3-33), one row per content
block, and the sealed row carrying the real `output_tokens` is never written. Measured on
wo-be05ab99's three subagents: 86 of 160 messages (57/123, 12/18, 17/19). The lead's own
transcript: zero such messages. Verified on
`/home/gonzalo/.claude/projects/-home-gonzalo-workspace-agentic-os--claude-worktrees-wo-be05ab99/c757152e-6c03-4902-a670-4279911175c4/subagents/agent-a28f7b4678b8a1473.jsonl`
— `msg_011CfpwEQUpoB6NKFAw7a5vz` has exactly two rows, both `stop_reason: null`, both
`output_tokens: 5`, one `text` block and one `tool_use` block, no third row. The true
output of that call is **not recoverable from the file**.

`usage._assistant_messages` (`src/jarvis/usage.py:592-637`) is already correct per
kn-2137076d: dedupe by `message.id`, merge by MAX per field. Nothing better exists to
take. The dedupe and the MAX merge are NOT in scope and must not change.

**Defect (a) — a running turn reads as a confident low figure.** A turn with no result
JSON yet is charged from the transcript alone (`bill._turn_items`,
`src/jarvis/bill.py:488-527`, the `turns still running` branch). Its output is
systematically low and the line says nothing about it.

**Defect (b) — the shortfall is charged to the lead agent.** A sealed turn takes its
total from the CLI result envelope's `modelUsage`. `bill._agent_items`
(`src/jarvis/bill.py:635-713`) treats the turn totals as a budget, lets each subagent draw
its transcript slice per model and per class, then hands the lead the whole remainder:

```python
for model, left in budget.items():           # src/jarvis/bill.py:671-673
    if any(left.values()) or not drafts:
        drafts.append((LEAD, model, dict(left), "lead"))
```

So every output token a subagent's placeholder rows failed to report is charged to the
lead. On wo-be05ab99 turn 2 the lead row shows 69,460 output tokens against its own
transcript's 17,421. `bill.reconcile` prints balanced throughout, because the fold still
sums to the turn — the money is in the right turn and on the wrong agent.

The turn row already computes the honest lead figure and publishes it:
`row["calls_cover"]` in `_attach_calls` (`src/jarvis/bill.py:620-624`) is the lead agent's
calls only, per class. Nothing reads it when the lead row is built.

## The fix

Five mechanisms. Decisions 1-5 are the user's ruling on Neo question 1500, not open here;
what follows is where each lives and what every degenerate case does.

### 1. Produce the placeholder flag in `_assistant_messages`, carry it on the dataclasses

`_assistant_messages` (`usage.py:592`) is the only place that sees all rows for a message
id, so the flag has to be produced there. `stop_reason` lives on `message`, not on
`message.usage`, so the existing `for key, value in usage.items()` merge cannot reach it —
add an explicit accumulation beside the `cache_creation` flatten:

```python
entry["sealed"] = entry.get("sealed", False) or message.get("stop_reason") is not None
```

Set it on the first-occurrence branch too (`entry = by_id[mid] = {...}`), alongside
`model` and `ts`, so the key is always present. `sealed` cannot collide with a `usage`
key (`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`,
`output_tokens`, `service_tier`, `cache_creation`).

`_is_api_call` (`usage.py:646`) stays as it is: a placeholder row carries non-zero
`cache_read`/`cache_write`, so it passes, and it SHOULD — it is a real API call whose
output figure alone is unreliable.

Then, field by field:

* `usage.Call` (`usage.py:389-435`) gains `sealed: bool = True`, and `"sealed"` in
  `as_dict()` so it reaches `turn_rows[*].call_rows`. Default True: every other producer
  of a `Call` is reporting a figure it believes.
* `usage.calls_of` (`usage.py:705`) passes `sealed=message.get("sealed", True)`.
* `usage._call_of` passes the same, so the boundary classifier and `_usage_of` see one
  shape.
* `usage.Usage` (`usage.py:203`) gains two additive fields, both summed in `__add__` and
  both in `as_dict()`:
  * `placeholder_calls: int = 0` — messages whose every row was unsealed.
  * `placeholder_output: int = 0` — the placeholder output figures those messages DID
    report, so a surface can say "N calls report M tokens of output, the true figure is
    higher" without inventing the difference.
* `usage._usage_of` (`usage.py:933`) increments both in the per-message loop.
* `usage.read_session` (`usage.py:1068`) needs no change: it sums `Usage`s, so
  `SessionUsage.main`, `.subagents`, `.total` and each `Subagent.usage` carry the counts
  for free, and `SessionUsage.as_dict` nests them.

NOT DONE, on purpose (decision 3): nothing estimates or synthesises the missing output
from content length. Asserting a number no transcript supports is the defect this fix
exists to stop. `usage.output` keeps the MAX-merged placeholder value.

### 2. The lead is charged its own calls; the residue gets a sibling row

`_attach_calls` (`bill.py:556-632`) gains one additive turn-row key beside
`calls_cover`, because the budget drawdown in `_agent_items` is per model and a flat
cover cannot feed it:

```python
row["calls_cover_by_model"] = {model: {cls: sum(...) for cls in TOKEN_CLASSES}
                               for model, group in by_model(found)}
```

keyed on `call.model`. Set it in the same block, under the same condition (`found`
non-empty) — its presence is exactly "this turn's lead calls were found in the
transcript", which is the signal §4 below branches on.

`_agent_items` (`bill.py:635`) then replaces the remainder loop at `bill.py:671-673`:

1. **No subagents on this turn** (`not subs`): take the current path unchanged — the whole
   remainder to the lead, one draft per model, same note, same `calls=1`, no unattributed
   row, byte-identical items. This is the common case and the regression risk. It is also
   correct by cause: with no subagent the only things the lead's transcript cannot cover
   are side-model calls that write no assistant message and CLI self-compaction (both
   measured, see `CALL_COVER_SLACK`, `bill.py:1439-1460`), and both are the lead's own
   spend. Splitting them onto a residue row would relabel ordinary lead spend as
   unattributed on every single-agent turn.
2. **Subagents and a lead cover** (`row.get("calls_cover_by_model")` present): per model,
   `lead_take[cls] = min(left[cls], mine.get(cls, 0))`; decrement `left`; what remains in
   `left` is the residue. A transcript model name absent from `budget` falls back to
   `next(iter(budget), "unknown")`, the same fallback the subagent path already uses
   (`bill.py:657`).
3. The residue, where any class is non-zero, becomes a draft `(UNATTRIBUTED, model,
   dict(left), "unattributed")` — one per model with a residue, for the reason the lead
   gets one per model: output is the dearest class and a residue priced in the wrong
   model's band is a wrong dollar figure.

Two new module constants beside `LEAD`/`LEAD_NOTE`/`SUBAGENT_NOTE` (`bill.py:427-434`),
worded once and read by both renderers (the `SUBAGENT_REWRITE_ZERO` rule, `bill.py:148`):

* `UNATTRIBUTED = "unattributed"` — the row label.
* `UNATTRIBUTED_NOTE` — names the cause: tokens the turn's envelope reports that neither
  the lead's own API calls nor its subagents' transcripts account for; a subagent stores
  most assistant messages as mid-stream snapshots whose `output_tokens` is a streaming
  placeholder and whose sealed row is never written, so the shortfall is mostly output and
  cannot be attributed to an agent. It says the row is a residue, not a measurement, and
  carries the placeholder count where one is known.

**Charged no count, and no unit of its own**: `calls=0`, `unit="turn"`. Consistent with
the rule already stated in `_agent_items`' docstring and enforced by `charged_turn`
(`bill.py:688-696`) — the countable act on a worker line is the TURN, a subagent is not a
second act inside it, and a residue is less of one still. `_add_item` only touches
`line["unit"]` when `item.calls` is non-zero (`bill.py:327-329`), so a zero-count row
cannot blank the parent's unit.

**`named`** (`bill.py:684-685`) becomes true whenever an unattributed draft exists, which
it already does: `labels` is the set of draft labels, and a residue row means there is
something to distinguish from the lead.

**The exact-dollar split** (`exact_usd`, `bill.py:707) needs no structural change: the
residue is a third entry in `drafts`, priced at list by `usage_mod.class_costs` like the
other two, and takes `exact * list_usd / total_list`. The shares still sum to the turn's
one exact figure exactly. Two orderings to preserve: subagent drafts first, then lead,
then unattributed — so `first = i == 0` keeps putting the turn's `cache_1h`/`cache_5m`
split on the same row it rides today, and the residue (which has no TTL split of its own)
never becomes row zero.

`_agent_view` (`bill.py:833`) folds on `i.inner[1]`, so the residue becomes its own agent
line with no change. Extend its sort key to
`(line["label"] != LEAD, line["label"] == UNATTRIBUTED)` so the list reads lead, then
subagents, then residue.

### 3. Degenerate cases, each with its required behaviour

| case | behaviour |
|---|---|
| turn with no subagents | no unattributed row, no new key read, items identical to today |
| subagents, residue zero in every class | no unattributed row (the `any(left.values())` guard) |
| lead transcript missing or pruned | §4 |
| lead transcript exceeds its envelope share | §5 |
| subagent whose model is not in the budget | unchanged — existing `next(iter(budget))` fallback, then existing `stranded` |
| subagent with zero tokens | unchanged — `_subagents_by_turn` already drops it (`bill.py:546-549`) |
| turn not `recorded` | unchanged — `_turn_items` skips it (`bill.py:461`) and its spend is the gap line, where §6 applies |

### 4. A pruned lead transcript: the old behaviour is the only option, and it says so

`calls_cover_by_model` absent on a recorded turn means Claude Code has pruned or
part-pruned the lead's segment, or no call could be placed in the turn's window
(`row["calls_source"] == "envelope"`). There is then no reliable lead figure, so the
remainder-to-the-lead behaviour stands — no unattributed row, because splitting the
remainder would need the very figure that is gone.

It must not read as measured. On that row only, the note becomes a new constant
`LEAD_UNMEASURED_NOTE`, replacing `LEAD_NOTE`: the lead's share is what is left of the
turn after its subagents, because the lead's own transcript is gone, so anything its
subagents under-reported is inside this figure rather than beside it. Same mechanism as
the existing `ABSENT_NOTES` doctrine (`bill.py:126-145`): absent is not zero, and a
figure derived by subtraction is not a measurement.

### 5. The lead clamp, and the `stranded` path

The `min()` in §2.2 clamps a lead cover that exceeds its envelope share. The clamped
excess must not vanish — it is the same direction `_check_calls` already calls a defect
(`bill.py:1490-1495`) and `stranded` is the existing vocabulary for "a transcript claims
more than the turn contains" (`bill.py:654-668`, note at `bill.py:482-487`).

The existing note is worded about subagents and would be a lie about a lead excess, so
`_agent_items` returns `(items, stranded)` with `stranded` a
`dict[str, int]` of `{"subagent": n, "lead": n}` instead of one int. `_turn_items`
(`bill.py:479-487`) accumulates both and appends one sentence per non-zero side: the
existing wording for the subagent side verbatim, and a new one for the lead side saying
its own API calls total more than the turn's envelope reports, so the excess stays inside
the turn rather than being added to it. Same rule, said about the right thing.

### 6. A running turn declares its placeholder calls

Two surfaces, because the live page and the terminal both read a running turn.

* `_worker_extras` (`bill.py:1332`) gains a `"placeholder"` block beside `"subagents"`:
  `{"calls": total.placeholder_calls, "output": total.placeholder_output,
  "main_calls": session.main.placeholder_calls,
  "subagent_calls": session.subagents.placeholder_calls}`. Split by side for the reason
  `rewrite.by_side` is (`bill.py:1368-1383`): the lead side is zero by shape and the
  subagent side is the finding.
* The running-turn gap item in `_turn_items` (`bill.py:509-513`) appends a new constant
  `PLACEHOLDER_NOTE` to its note when `session.total.placeholder_calls` is non-zero:
  N calls report placeholder output totalling M tokens, so this figure is a floor; the
  sealed row carrying their real output is never written, and nothing here estimates it.
  It goes on the line that is actually low, not only in a summary block.

### 7. Surfaces

| surface | file | change |
|---|---|---|
| `jarvis cost` tree | `cli._print_bill` → `cli._print_bill_line`, `src/jarvis/cli.py:1936-1952, 2022-2069` | none needed — the residue is an ordinary line in `actors`/`turns`/`agents`; `calls=0` already suppresses the count |
| `jarvis cost` captions | `src/jarvis/cli.py:2142-2148` | print the placeholder sentence beside the existing subagent sentence, from `bill_mod.PLACEHOLDER_NOTE`, only when `bill.get("placeholder")` is present |
| `jarvis cost` per-call table | `cli._print_turn_calls`, `src/jarvis/cli.py:1985-2019` | the "the rest of turn N is the subagents it spawned" line at `:2018` is now wrong by omission — reword to name both the subagents and the residue |
| `jarvis cost --json` | `src/jarvis/cli.py:3044-3047` (`_print(bill, True)`) | none — the payload is dumped whole, so the new keys appear automatically |
| dashboard bill page | `src/jarvis/ui/app.py:1492-1510` → `src/jarvis/ui/templates/bill.html` + `_bill.html` | notes already render (`_bill.html:94`); the "Every API call" caption at `bill.html:149-155` and the "Agent by agent" caption at `bill.html:240-249` both assert the difference is subagents and must name the residue too |
| dashboard globals | `src/jarvis/ui/app.py:857-863` | pass `placeholder_note=bill.PLACEHOLDER_NOTE` beside `subagent_rewrite_zero`, so the sentence has one source |
| WO page bill panel | `src/jarvis/ui/app.py:552-570, 1370-1398` | none — reads the same payload |

### 8. `reconcile`: a lead row may never exceed the lead's own transcript

New `bill._check_lead`, called from `reconcile` (`bill.py:1398-1436`) beside
`_check_agents` and `_check_calls`. The by-turn fold keys a line as
`"/".join(path)` over `(seq, WORKER, label)`, so the lead row under turn 3 has key
`3/worker/the lead agent`. For each recorded turn row carrying `calls_cover`, find the
descendant of `payload["turns"]` whose key ends `f"/{WORKER}/{LEAD}"` and assert, per
class, `lead["tokens"][cls] <= cover[cls]`. Problem wording:
`"turn {seq}: the lead agent is charged {n} {cls}, more than its own API calls report ({m})"`.

Skipped, deliberately, where it cannot speak: a turn with no `calls_cover_by_model` is the
§4 fallback, where the lead legitimately holds the remainder, and a turn that is not
`recorded` has no envelope to partition. Same discipline as `_check_calls`' own
"asked only of a conversation the transcript accounts for entirely"
(`bill.py:1477-1481`).

### 9. `PAYLOAD_VERSION` stays at 5 — the decided tradeoff

Do NOT bump `bill.PAYLOAD_VERSION` (`bill.py:93`). `_upgrade_seal` (`bill.py:894`) gates
re-sealing on `(sealed.get("payload_v") or 1) >= PAYLOAD_VERSION`, so a bump marks every
sealed bill stale and re-reads transcripts that may have expired. Where the evidence is
gone the upgrade is abandoned and the seal stands — but the cost is a full transcript walk
per sealed order on first read, and where the evidence has PART-expired the re-read turns
an already-correct sealed bill into a permanent unknown. That is a worse loss than a
missing breakdown on historical turns (kn-938b1878).

So the new keys are additive at version 5:

* turn rows: `calls_cover_by_model`
* payload: `placeholder`
* `Call.as_dict()`: `sealed`
* new agent rows: appear only where a bill is computed live or re-sealed for another reason

**A reader seeing any of them ABSENT on an older sealed bill must read it as UNKNOWN, not
zero.** Enforced in the renderers by testing PRESENCE, never truthiness:
`if bill.get("placeholder") is not None`, so "0 placeholder calls, measured" and "this
seal predates the measurement" render differently, and the second renders as nothing
rather than as a zero. State this in the `#:` block under `PAYLOAD_VERSION` beside the
existing version history, as the version-4 entry already states the opposite convention
for `compact_write` (`bill.py:85-88`) — the two cases differ and both have to be written
down.

## Rejected alternatives

* **Estimate the missing output from content length** (characters of `text` plus
  serialised `tool_use` input, divided by a tokens-per-char constant). Rejected by
  decision 3: it puts a number on the bill that no transcript supports, on the dearest
  token class, and it would be indistinguishable from a measurement on every surface.
  `usage.BASIS_CHARS` exists for a char-proportional split of an EXACT total — there is no
  exact total here.
* **Leave the remainder on the lead and add a caveat.** The lead row is the figure a reader
  acts on ("my lead agent is writing 69k tokens a turn"); a caveat elsewhere does not
  unmake a wrong number in the place they look.
* **Change the MAX merge in `_assistant_messages` to sum the per-block rows.** The rows
  repeat the same `usage` object; summing is how the first measurement of wo-cd73c537 read
  2.7M cache-write against a true 1.03M (kn-2137076d). Explicitly out of scope.
* **Scale the placeholder outputs up by the observed ratio of sealed to unsealed calls.**
  A synthesised number again, and the ratio is measured on a different population from the
  one it would be applied to.
* **Bump `PAYLOAD_VERSION`** so historical bills gain the breakdown. §9.
* **Fix it in `claude_cli` by re-reading the result envelope for subagents.** There is no
  result envelope for a background subagent; the transcript is the only record, and the
  sealed row is never written to it. The root cause is upstream in Claude Code's
  transcript writer and is not ours to fix — this spec is an honest accounting ON TOP of a
  defect it cannot repair, and the unattributed row is the name for exactly that.

## Test plan

Suites: `tests/test_bill.py` (the arithmetic identities; it imports its builders from
`tests/test_cost_report.py:29-32`), `tests/test_usage.py` (the parser),
`tests/test_bill_evidence.py` (`test_render_a_bill_with_every_actor_on_it`, `:32`),
`tests/test_ui_cost.py` (the page).

Existing builders to reuse, not re-create:
`tests/test_cost_report.py:20 assistant_row`, `:40 transcripts` (its `subagents` argument
takes a list of row-lists, or `(rows, meta)` tuples where the meta names the row),
`:101 give_session`, `:213 recorded_usage` (the `usage_json` envelope),
`:222 add_turn`; and `src/jarvis/testing.py` `FleetFixture.call_row` (`:2912`),
`.transcript` (`:2960`), `.turn` (`:2834`), plus the fake CLI envelopes at `:331-345` and
`:568-584`.

**The key new fixture.** `placeholder_rows(mid, *, read, write, out=5)` in
`tests/test_cost_report.py` beside `assistant_row`: returns the TWO rows Claude Code
writes for one message id — identical `usage` with the placeholder `output_tokens`, both
with `stop_reason: None`, one carrying a `text` content block and one a `tool_use` block,
and no sealed third row. `assistant_row` gains `stop_reason: str | None = "end_turn"` so
every existing row in both suites stays SEALED and no existing expectation moves. Mirror
both in `testing.py:call_row` (same default) and add `FleetFixture.placeholder_call_rows`
so the browser and eval suites can stage one.

Failing-first, one per decision:

1. `test_the_lead_is_charged_its_own_calls_and_not_the_remainder` — one recorded turn whose
   envelope reports 100k output; lead transcript three sealed calls summing 20k output; one
   subagent transcript that is placeholder-only. Asserts the lead agent line's `output` is
   20k (today: ~85k) and `agents` still sums to the worker actor line in every class.
2. `test_what_no_transcript_accounts_for_is_its_own_row_not_the_leads` — same staging.
   Asserts a line labelled `bill.UNATTRIBUTED` exists under that turn carrying the residue,
   its note is `bill.UNATTRIBUTED_NOTE`, its `calls` is 0, its model is the envelope's, and
   `reconcile(payload)["balanced"]` is true with `sum(agents) == worker` per class and on
   `total`.
3. `test_a_turn_with_no_subagents_is_unchanged` — the regression guard. Same order with no
   subagent directory: no line labelled `UNATTRIBUTED` anywhere in `actors`, `turns` or
   `agents`, and the lead line's tokens equal the turn envelope exactly in all four classes.
4. `test_a_pruned_lead_transcript_still_gives_the_lead_the_remainder_and_says_so` —
   subagent transcript present, lead segment with no call placeable in the turn window, so
   `calls_source == "envelope"`. Asserts no unattributed row, the lead holds the remainder,
   and its note is `bill.LEAD_UNMEASURED_NOTE`.
5. `test_a_lead_transcript_bigger_than_its_turn_cannot_inflate_the_bill` — lead calls
   summing above the envelope. Asserts the lead row is clamped to its envelope share, the
   totals are unchanged, and `payload["notes"]` carries the lead-side strand sentence.
   Sibling of the existing `test_a_subagent_bigger_than_its_turn_cannot_inflate_the_bill`
   (`tests/test_bill.py:290`).
6. `test_a_running_turn_declares_its_placeholder_calls` — one running turn plus a
   placeholder-only subagent. Asserts `bill["placeholder"]["calls"]` and
   `["subagent_calls"]` equal the staged count, the running-turn line's note contains
   `bill.PLACEHOLDER_NOTE`, and — the half that guards decision 3 — the line's `output`
   still equals the MAX-merged transcript sum, i.e. nothing was estimated.
7. `test_a_lead_row_can_never_exceed_the_leads_own_transcript` — hand `reconcile` a
   hand-built payload whose lead row is inflated past its `calls_cover`, the way
   `test_a_turn_bigger_than_everything_inside_it_fails_the_checks`
   (`tests/test_bill.py:442`) does, and assert the exact problem string; then the honest
   payload balances; then a payload with no `calls_cover_by_model` produces no problem.
8. `tests/test_usage.py::test_a_message_whose_every_row_is_mid_stream_is_counted_not_estimated`
   — two placeholder rows for one mid plus one sealed mid. Asserts
   `usage.placeholder_calls == 1`, `placeholder_output == 5`,
   `calls_of(path)[i].sealed is False` for the placeholder and True for the sealed one, and
   `usage.output == 5 + sealed_out` — the MAX-merge result, unchanged, which is what fails
   if someone later "fixes" this by scaling.
9. `tests/test_bill_evidence.py` and `tests/test_ui_cost.py` — `jarvis cost` and the page
   both show the residue row and the placeholder sentence on a live bill; and an older
   sealed payload with no `placeholder` key renders neither and prints no zero (§9's
   UNKNOWN-not-zero rule). The dashboard half asserts the reworded captions at
   `bill.html:149-155` and `:240-249`.
10. `test_an_old_seal_is_not_restaled_by_the_new_keys` — seal a bill at `PAYLOAD_VERSION`
    5, then assert `bill._upgrade_seal` returns None and the seal is served untouched after
    this change, i.e. no transcript is re-read. Pairs with the existing
    `test_an_old_seal_is_re_derived_while_the_evidence_survives` (`:1099`) and
    `test_an_old_seal_stands_once_the_evidence_is_gone` (`:1137`).

Full suite before the PR: `uv run pytest tests/ evals/`.
