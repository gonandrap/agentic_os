# A request in flight is not a stalled turn

Work order wo-9b3e391f. Supersedes the premise of issue #823: the turn it was filed about
was generating, not dead.

## The problem

### 1. "No API call" is inferred from the absence of a transcript row, and the absence is
not evidence

Claude Code appends an assistant row only when a CONTENT BLOCK COMPLETES. While a request
streams, the transcript's last row is the user prompt (or a `tool_result`) and nothing
follows it.

Measured on wo-dbea82cf, 2026-09-26 PDT (user's forensics, ground truth):

| 21:32:48 | the turn prompt (a Neo answer) is written |
| 21:38:51 | request `req_011CfVDFz9` completes its FIRST block (thinking, then text) |
| 21:51:37 | the SAME request completes its next block, an `Agent` tool_use |

4,057 output tokens in ~19 minutes, ~3.5 tok/s — API-side slowness right after the usage
window reopened. TLS connections to `160.79.104.10:443` were open throughout.

Every cost layer reads calls off completed assistant rows: `usage.calls_of`
(src/jarvis/usage.py:692-712) iterates `_assistant_messages`, `inspection._attach_calls`
(src/jarvis/inspection.py:1232-1248) files those into turns, and `Turn.observed`
(src/jarvis/inspection.py, `observed` property, body at 548-721) is `bool(self.calls)`.
So for those 19 minutes `observed` was `False`, and four surfaces turned that into a claim
about money and about work:

- src/jarvis/inspection.py:1496-1501 — `STALL_ALARM` raised past
  `alarm_stalled_minutes` (15), reason: `"… has made no API call at all — the work never
  started, and nothing has been spent on it"`.
- src/jarvis/inspection.py:1361 — the legend: `"a turn open with no API call ever made —
  nothing is being billed"`.
- src/jarvis/supervisor.py:366 —
  `stalled = "" if turn.observed else "NO API CALL WAS EVER MADE — it cost nothing. "`,
  prepended to the turn's line in the evidence packet. **This is the line that fed the
  judge.**
- src/jarvis/supervisor.py:121-124 (`ALARM_REVIEWER_PERSONA`) — "A turn with no API call
  bought nothing however long it ran — it stalled".
- src/jarvis/cli.py:1790 `PART_LABELS["unaccounted"] = "unaccounted — no API call was ever
  made"`, and src/jarvis/cli.py:1806-1809 `NO_CALL_FLAG = "NO API CALL"`.

The alarm became a supervisor escalation and reached the user as Neo question 883. A kill
remedy acting on that packet would have destroyed 19 minutes of live generation.

`src/jarvis/live.py` is the one surface that already gets the STATE right:
`_state` (src/jarvis/live.py:582-599) returns `GENERATING` whenever the caller says a turn
is in flight and no span is open, and its comment at :670-672 names the blind spot
exactly. Only its sentence is thin — `_note` (src/jarvis/live.py:669-673) says `"a turn is
in flight and nothing has been written since …"`, which reports the silence without naming
what the silence is.

### 2. The stalled-turn alarm escalates to the user

`Daemon.check_burning_turns` (src/jarvis/daemon.py:4023-4051) writes every raised alarm
through `store.add_alarm` (src/jarvis/project_store.py:3012-3025, status defaults to
`'raised'`) and flags attention at :4050-4051. `claim_next_alarm`
(src/jarvis/project_store.py:3172-3188) claims any `status='raised'` row, `_drain_project_
alarms` (src/jarvis/daemon.py:3693-3713) hands it to `supervisor.review`, and an escalation
lands as an inbox row plus an attention flag at src/jarvis/daemon.py:3395-3407. That is the
full path from a false `observed=False` to an interruption.

### 3. Turn numbering is off by one, measured

Turn files under `.jarvis/turns/wo-dbea82cf/` spawn at:

```
1 07:36:18  2 07:40:27  3 08:31:27  4 08:40:05  5 11:31:58  6 11:33:53
7 19:38:39  8 19:39:49  9 20:08:48  10 21:10:09  11 21:32:44
```

`jarvis inspect` reports TEN turns:

```
1 07:36:22  2 07:40:30  3 08:31:31  4 08:40:09  5 11:33:49
6 19:38:43  7 19:39:53  8 20:08:52  9 21:10:13  10 21:32:48
```

File 5 (11:31:58) was the `/compact` turn. Compaction rewrites the transcript, so its
prompt does not survive as a distinct prompt row — inspect turn 5 (11:33:49) carries four
triggers including the compaction summary and the `/compact` command. Every later number
shifts by one. The alarm numbers from `wo_turns.seq` and said **turn 11** while `jarvis
inspect` said **turn 10**, about the same turn.

Root cause: `read_session` renumbers transcript turns 1..N in isolation
(src/jarvis/inspection.py:1163-1165), over turns minted with `seq=len(turns)+1`
(src/jarvis/inspection.py:1065). Nothing binds that sequence to the OS's own. The hazard is
already written down in src/jarvis/ops.py:10094-10098: `context_report` joins writes to
turns BY TIMESTAMP and says in its docstring that inspect's numbering "is not
`wo_turns.seq`". One consumer worked around it; no consumer fixed it.

## The fix

Three changes, all in `inspection` plus its callers. Nothing kills a process (see
Non-goals).

### 1. A carrier for "awaiting the model", derived in the walk `read_transcript` already does

`src/jarvis/inspection.py:1027-1095` is a single ordered walk. It already tracks
`saw_assistant` (:1041, set at :1071-1072). Add one more piece of state to the same walk:

- on a prompt row (:1058-1070) and on any row carrying a `tool_result` block (:1090-1093),
  set `open_turn.awaiting_since = ts`;
- on `row["type"] == "assistant"` (:1071-1072), set `open_turn.awaiting_since = 0.0`.

At the end of the walk a turn has `awaiting_since > 0` exactly when its last input row has
no assistant row after it. No second pass, no new file read.

New on `Turn` (dataclass at src/jarvis/inspection.py:548-721):

```python
awaiting_since: float = 0.0          # field, beside active_ended

@property
def awaiting(self) -> bool:          # "a request is in flight, no block has completed"
    return bool(self.awaiting_since)
```

`Turn.as_dict` gains `"awaiting"` and `"awaiting_since"`, ADDITIVE — no existing key reads
them, the same rule `"subagents"` was added under.

**`inspection` still must not open the OS database.** `awaiting` is derived only from rows
Claude Code wrote, which is `read_session`'s standing rule about `spans` being passed in
(src/jarvis/inspection.py:1141-1147). Liveness of the process is the CALLER's fact:
`Daemon.check_burning_turns` already has it (`turn["state"] != "running"` →
`continue`, src/jarvis/daemon.py:4020-4022), and `live.snapshot` already takes
`turn_in_flight` for precisely this reason (src/jarvis/live.py:545-556).

One formatter, so every surface says the same sentence:

```python
def awaiting_note(turn: Turn) -> str:
    "awaiting the model since 21:32 (request in flight, no block completed)"
```

Call sites, each replacing a false claim:

| Site | Today | After |
|---|---|---|
| src/jarvis/inspection.py:1496-1501 | `STALL_ALARM` reason | see §2 — no live raise, and the `slow-model-response` reason opens with `awaiting_note` |
| src/jarvis/inspection.py:1361 | `ALARM_KINDS[STALL_ALARM]` | kept verbatim for historical rows, plus a new entry for the new kind |
| src/jarvis/supervisor.py:366 | `"NO API CALL WAS EVER MADE — it cost nothing. "` | branch on `turn.awaiting` FIRST: awaiting → `awaiting_note(turn) + " — nothing has COMPLETED yet; this is not a stall. "`; only a turn that is neither `observed` nor `awaiting` keeps the old sentence |
| src/jarvis/supervisor.py:121-124 | the judge's "it stalled" paragraph | add the distinction: a turn whose last row is an input with nothing after it is AWAITING; never describe it as stalled, as costing nothing, or as never started |
| src/jarvis/cli.py:1790 | `"unaccounted — no API call was ever made"` | `"unaccounted — no API response has completed"`. KEY UNCHANGED: `PART_LABELS` is pinned equal to `inspection.PARTS` (comment at cli.py:1786-1787) |
| src/jarvis/cli.py:1806-1809 | `NO_CALL_FLAG` printed whenever `not observed` | print it only when `not observed and not awaiting`; when awaiting, print a new `AWAITING_FLAG = "AWAITING MODEL"` of the same fixed width |
| src/jarvis/live.py:669-673 | `"a turn is in flight and {silence}"` | `f"{awaiting-words}; {silence}"`. **Verified: live's STATE is already correct** — `_state` (:582-599) returns `GENERATING`, never `IDLE`, while a turn is in flight, so only the sentence changes |

### 2. `slow-model-response`, informational, and out of the escalation path

In `src/jarvis/inspection.py` (beside the constants at :1318-1320):

```python
SLOW_RESPONSE_ALARM = "slow-model-response"
#: Kinds that are a NOTE, not an interruption: recorded, rendered, never escalated.
INFORMATIONAL_KINDS = frozenset({SLOW_RESPONSE_ALARM})
```

`ALARM_KINDS` gains `SLOW_RESPONSE_ALARM: "a request has been in flight this long with no
completed content block — the model is answering slowly"`. `STALL_ALARM` and its entry
STAY, so historical rows on `/alarms` and in `jarvis alarms` still render through
`ops.alarm_kind_label` (src/jarvis/ops.py:2101-2118); its legend is reworded to past tense
("was open with no API call ever made") since nothing raises it live any more.

`alarms()` (src/jarvis/inspection.py:1496-1506) replaces the `if not turn.observed` branch:

1. `turn.awaiting` → raise `SLOW_RESPONSE_ALARM` only when
   `active_awaiting >= cfg.alarm_awaiting_minutes * 60`, where `active_awaiting` is
   `now - turn.awaiting_since` minus the holds overlapping that window — the same active
   clock the other duration alarms use (:1481-1488, the user's 2026-09-18 ruling). Below
   the threshold, NOTHING is raised.
2. neither `observed` nor `awaiting` → raise nothing at all. `STALL_ALARM` is never raised
   live. The evidence for "the work never started" was the absence of a row, and that
   absence is exactly the signal this spec proves unreliable. A genuinely hung turn is
   still caught by `worker_session.TURN_STALL_SECONDS` (6h), which judges the PROCESS.
3. `observed` → `TURN_ALARM` as today, unchanged.

New setting in `src/jarvis/catalog.py`: `DEFAULT_INSPECT_ALARM_AWAITING_MINUTES = 60`
(beside :585-602), field `alarm_awaiting_minutes` on `InspectConfig` (:747-748) and a
parse line in `_parse_inspect` (:1495-1498). It inherits field-by-field and is refused at
zero by the loop at :1523-1533, like every other count. Default 60: the measured event was
19 minutes, so a threshold below the hour would re-raise the very case this spec exists to
stop reporting.

**What "must not escalate" means mechanically**, traced end to end:

- `ALARM_STATUSES` (src/jarvis/project_store.py:490-496) gains `"informational"`.
- `ProjectStore.add_alarm` (:3012-3025) gains `status: str = "raised"` and writes it.
- `Daemon.check_burning_turns` (src/jarvis/daemon.py:4037-4051) passes
  `status="informational"` for a kind in `inspection.INFORMATIONAL_KINDS`, and excludes
  those kinds from the `fresh` list that drives `flag_attention` at :4050-4051. The
  `cost_alarm` event is still written at :4043-4045, so the dedupe memory (`already`,
  :4033-4035) and the timeline are unchanged.
- `claim_next_alarm` (:3172-3188) selects `WHERE status='raised'`. An `informational` row
  is therefore never claimed → `supervisor.review` never runs on it → no Neo question, no
  `ESCALATED_INBOX_TITLE` row, no `ALARM_BLOCKER` attention flag
  (src/jarvis/daemon.py:3395-3407). No new filter is added anywhere; the queue's existing
  predicate is the enforcement.
- `reclaim_stale_alarms` (:3240-3266) only touches `status='reviewing'`, so nothing later
  promotes the row back into the queue.

Coupling kn-2bba079c, verified: `probes.RESERVED_IDS` (src/jarvis/probes.py:32-34)
duplicates `ALARM_KINDS` as literals because `probes` cannot import `inspection` without a
cycle through `catalog`. **`"slow-model-response"` must be added there.** Two tests pin the
two sets equal: tests/test_probes.py:225 and tests/test_cache_ttl_alarm.py:476.

Committed byte-for-byte literals that may move:

- tests/test_probes.py:36 `PROMPT_WITHOUT_PROBES` — the committed output of
  `supervisor.build_system_prompt`, i.e. `SUPERVISOR_PERSONA` (src/jarvis/supervisor.py:26-111).
  The persona edit above is in `ALARM_REVIEWER_PERSONA` (:112 onward), which is NOT in that
  prompt (it reaches a model through `neo.build_system_prompt`, src/jarvis/neo.py:175-179).
  So this literal holds unless `SUPERVISOR_PERSONA` is also touched — and it should not be.
- tests/test_supervisor.py:802 `EXPECTED_WORK_ORDER_PACKET`, asserted at :849 — the packet
  contains `_session_lines` output. Its fixture turn HAS calls, so the `awaiting` branch
  does not fire on it; if the literal moves, it moves and must be re-committed.
- tests/test_inspection.py:660-676 `test_the_defaults_are_the_measured_ones` and
  :759-761's zero-refusal parametrisation both want the new key added.

### 3. Bind transcript turns to the OS's own turn numbers

`read_session` (src/jarvis/inspection.py:1131-1188) gains one keyword, passed in exactly
the way `spans` is and for the identical reason — this module does not open the OS
database:

```python
turn_starts: Sequence[tuple[int, float]] = ()   # (wo_turns.seq, wo_turns.started_at)
```

Replacing the renumber at :1163-1165:

- With `turn_starts` EMPTY, numbering is 1..N as today. Every existing caller and test that
  does not pass it keeps its current answer, byte for byte.
- With pairs passed, each transcript turn takes the `seq` of the OS turn whose `started_at`
  is the LATEST one at or before `turn.started + TURN_BIND_TOLERANCE_SECONDS`.
- Tolerance: the turn file is written before the transcript's prompt row — measured offset
  on wo-dbea82cf is 3-4s (07:36:18 vs 07:36:22, 21:32:44 vs 21:32:48). The tolerance
  exists for the OPPOSITE skew only, so `TURN_BIND_TOLERANCE_SECONDS = 5.0` is declared as
  a module constant in `inspection` and added to the `allowed` set of
  `test_nothing_in_the_module_hard_codes_a_threshold` (tests/test_inspection.py:696-706),
  beside `NAMED_SESSIONS`: it is a measurement of write ordering, not a project's judgement
  about what is expensive, so it does not belong in the catalog.
- TWO TRANSCRIPT TURNS BINDING TO ONE OS TURN is the normal case, not an error — it is the
  compaction shape from §3 read forwards. Both keep that `seq`; `Turn.as_dict` gains
  `"part"` (1-based within its OS turn). `jarvis inspect` renders part 2+ as
  `turn 5 (continued)`.
- A TRANSCRIPT TURN THAT BINDS TO NOTHING — it starts before the first OS turn, which is
  what an injected or adopted session looks like (`jarvis wo inject`) — gets `seq = 0`.
  One formatter, `inspection.turn_name(seq)`, returns `"an unrecorded turn"` for 0 and
  `"turn N"` otherwise; used by the `jarvis inspect` renderer and by
  `supervisor._session_lines` (src/jarvis/supervisor.py:377-381). It is never `-1`:
  `project_store.NO_TURN` belongs to `wo_alarms` and `inspection` must not import a store.
- AN OS TURN WITH NO TRANSCRIPT TURN — the `/compact` case, file 5 above — is reported, not
  dropped: `Anatomy` gains `unmatched_os_turns: list[int]`, derived from the passed-in
  pairs, and `jarvis inspect` prints one line per entry: `turn 5 left no prompt row in the
  transcript (compaction rewrites it; its triggers are on turn 6)`.

Callers that must now pass the pairs. Add
`ProjectStore.turn_starts(wo_id) -> list[tuple[int, float]]` beside `all_turns`
(src/jarvis/project_store.py:3988-3998) — one indexed read, no JSON:

| Caller | Line | Note |
|---|---|---|
| `Daemon.check_burning_turns` | src/jarvis/daemon.py:4023-4030 | via `live_alarms`, whose signature (src/jarvis/inspection.py:1528-1545) gains the passthrough. It already holds `store` |
| `supervisor._session_lines` | src/jarvis/supervisor.py:350 | has `pstore`; degrades to 1..N when `pstore is None`, like `spans` at :348-349 |
| `ops.inspect_report` | src/jarvis/ops.py:9980 | has `store` at :9995/:10004 |
| `ops.context_report` | src/jarvis/ops.py:10112 | already read `store.all_turns(wo_id)` at :10109 — reuse those rows. Its docstring's warning at :10094-10098 is then obsolete and must be rewritten to say the two numberings now agree |
| `ops._diagnose_holds` | src/jarvis/ops.py:1767 | has `store` |

The dashboard needs NO direct change: `src/jarvis/ui/app.py` reaches every one of these
through `ops` (no `inspection.` reference in `src/jarvis/ui/`).

## Non-goals

- **No kill remedy is added, and none is implied.** The `remedies` registry stays the
  closed two (`nudge`, `unblock`). If one is ever added it may act ONLY on evidence of a
  dead process or a dead connection — no pid, no socket to the API, no child activity —
  and NEVER on "no transcript row yet", which is the exact signal this spec proves means
  the opposite of what it was read as.
- The `STALL_ALARM` detector is not deleted, only stopped from firing. Historical rows must
  keep rendering.
- Claude Code's transcript-write timing is not worked around any further: no polling of
  sockets, no `/proc` inspection. `awaiting` is what the file already says.

## Rejected alternatives

1. **Lower `alarm_stalled_minutes` / raise it to an hour and keep one kind.** Tempting and
   wrong: the finding differs, not the threshold. `stalled-turn` asserts nothing was spent;
   a slow response is spending output tokens right now. One kind makes "which one" a
   detail of a sentence instead of the identity that a dedupe, a filter and a learning key
   on — the same reasoning that split `rewrite-tax-prefix`/`-ttl`
   (src/jarvis/inspection.py:1332-1335).
2. **Keep raising `stalled-turn` but teach the supervisor's judge to spot the awaiting
   case.** That leaves the false claim in the DATA (`wo_alarms.reason`, the timeline, the
   attention line) and buys correctness from a model call. The judge already had the turn's
   real cost in front of it and still escalated; a persona is not a guard.
3. **Have `inspection` read `wo_turns` itself to get the numbering right.** Breaks the rule
   in `read_session`'s docstring (:1141-1147) that keeps this module callable from a test,
   a `--json` consumer and the daemon alike, and would put the OS database behind
   `jarvis inspect`.
4. **Renumber by counting turn FILES under `.jarvis/turns/<wo>/`.** The files are the right
   count but carry no prompt text, so nothing would bind a transcript turn to one; and it
   would make `inspection` depend on a project's on-disk layout.
5. **Merge a compaction's two transcript turns into one so the counts line up.** Changes
   the clock accounting — two prompt rows genuinely bracket two stretches of work — to make
   a number pretty.

## Tests required

1. An in-flight request — a user row with no assistant row after it, turn record `running`
   — renders as "awaiting the model", raises NO user-facing alarm, and triggers no kill:
   `Turn.awaiting is True`, `alarms(...)` returns `[]` below the threshold, and
   `supervisor._session_lines` output contains neither `"NO API CALL"` nor `"cost
   nothing"`.
2. Past `alarm_awaiting_minutes` the SAME anatomy raises exactly `[slow-model-response]`,
   and nothing escalates: after `check_burning_turns`, the `wo_alarms` row has
   `status='informational'`, `claim_next_alarm()` returns `None`, and the work order's
   `needs_attention` is still false.
3. `stalled-turn` is never raised live (a turn with neither calls nor an awaiting row
   raises nothing), while an existing `stalled-turn` row still renders on
   `jarvis alarms` / `/alarms`.
4. Turn numbers match the turn files on a session containing a compaction: a fixture with
   11 `wo_turns` rows and 10 transcript turns, one of the OS turns being the `/compact`
   one, yields transcript turns numbered `1..4, 6..11` with `5` in
   `Anatomy.unmatched_os_turns` — and the last turn is `11`, the number the alarm uses.
5. `read_session` with no `turn_starts` still numbers 1..N (the regression guard for every
   existing caller).
6. `set(probes.RESERVED_IDS) == set(inspection.ALARM_KINDS)` continues to hold with the new
   kind (tests/test_probes.py:225, tests/test_cache_ttl_alarm.py:476).
7. `alarm_awaiting_minutes` is settable per project and per fleet, inherits field-by-field,
   and is refused at zero (tests/test_inspection.py:719-767's three shapes).
