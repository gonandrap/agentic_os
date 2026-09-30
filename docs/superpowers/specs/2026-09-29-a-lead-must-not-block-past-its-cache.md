# A lead must not block past its cache

Work order wo-59c6f635, GitHub issue 868. Amends §4 of
docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md, which this document
inverts for three named command shapes and leaves standing for everything else.

## 1. The problem

**A lead that blocks inside its turn pays for the whole conversation again when it comes
back, and the OS's only remedy runs between turns, where the gap is not.**

Measured over the last 25 production work orders — 163 lead turns, $623 of spend:

| Fact | Value |
|---|---|
| `ttl-expiry` cache re-writes that happened MID-TURN, while the lead was blocked | 85 |
| Tokens re-written by them | 9.1M |
| Cost | ~$52, 8.4% of fleet spend |
| Longest gap that caused one | under 60 min — every one of the 85 |
| Median gap | 9.3 min |
| Foreground `Agent` subagent joins | 55 writes, $33.5 |
| Foreground shell calls | 28 writes, $17.4 |
| Worst orders | wo-4beada49 $11.8, wo-dbea82cf $11.1, wo-37f2fc2c $8.3 |

The 28 shell writes are three shapes and only three: whole-suite test runs, CI check
watchers (`gh run watch`, `gh pr checks --watch`), and hand-written sleep/poll loops.

**The mechanism.** Every Jarvis call buys the 5-minute prompt cache
(`claude_cli.PROMPT_CACHE_5M_ENV`, `inspection.TTL_5M`). A call more than `TTL_5M` after
the previous one is labelled `TTL_EXPIRY` by `inspection._writes`
(src/jarvis/inspection.py:1173) and re-sends the whole conversation at the cache-WRITE
rate, 1.25x input. At a 150k context that is ~$0.56 a time on Opus pricing; 85 of them is
the $52.

**Why nothing catches it.** `worker_session.compact` runs BETWEEN turns, off
`Daemon`'s relaunch path. A gap that opens at minute 4 of a turn and closes at minute 13
is invisible to it: no turn boundary, no compaction, nothing to shorten. The OS can
already SEE the gap — `inspection.raise_alarms` (src/jarvis/inspection.py:1681) fires
`JOIN_ALARM` at `alarm_join_seconds` and says "long enough to lose the prompt cache, so
the wait will be paid for twice" — and then has nothing to do about it. It reports the
bill; it does not prevent it.

**And the briefing asks for exactly this.** `worker_brief.core_contract`
(src/jarvis/worker_brief.py:262) and the standing-instructions section at
src/jarvis/worker_brief.py:514 both say: *already backgrounded something? It is already
lost — re-run it in the FOREGROUND and wait.* `hooks.background_task_decision`
(src/jarvis/hooks.py:435) enforces it with a `_deny` whose text is "Wait for the command
here instead — the fix costs you nothing but the wait". The wait is the $52. **The root
cause is not worker discipline: the OS instructed the behaviour and enforced the
instruction at the tool call.** §4 of the 2026-09-23 spec was right about the class it
closed (a job abandoned at turn end, issue #575, 62 hours of wall clock on wo-d81fcc15)
and wrong about the remedy it prescribed for everything else.

### Rejected alternatives, with their arithmetic

- **Buy the 1-hour cache fleet-wide.** Every write goes from 1.25x to 2x input. The 85
  ttl-expiry writes disappear; every OTHER write in the same 163 turns — cold starts,
  compaction writes, prefix misses, and each turn's own delta writes — gets 60% dearer.
  Net on the measured period: **~$40 MORE, +6%**. It also cannot be declared honestly per
  project: `inspection`'s TTL constants are "durations Anthropic's prompt cache actually
  offers (kn-f94abf34), not a policy Jarvis gets to hold an opinion about"
  (src/jarvis/inspection.py:96).
- **End the turn and let the seat run detached.** Re-opens the orphaned-background-task
  class §4 of the 2026-09-23 spec closed: the task outlives the turn and nobody collects
  it. Latest case is the long-join alarm on wo-8a68e024 turn 8. `background.py`'s whole
  existence is the cost of that class, and `background.NUDGE_MAX` re-sends are two extra
  turns per occurrence.
- **Compact mid-turn.** No mechanism exists. `PreCompact` cannot inject context and the
  OS cannot drive a compaction into a `claude -p` process it is not talking to.

## 2. The fix

**The lead never blocks longer than ~4 minutes inside one tool call. Long work runs in the
BACKGROUND, the lead checks in every 3-4 minutes with a bounded collect call, and the Stop
hook refuses to end the turn while anything is uncollected.**

Neo, question 1047, the user's ruling verbatim: *"A plus the Stop guard. Also: in this PR,
change section 4 of the spec, the worker-brief bullet and the standing-instructions text
to describe the carve-out, and append a knowledge-base entry that supersedes kn-6dcaf055.
The Stop guard must cover both Bash and Agent background tasks, and needs a test proving
it refuses to end the turn while a task is uncollected."*

The arithmetic of the new rhythm: a check-in 3-4 minutes after the previous call is inside
`TTL_5M`, so it is a cache READ at 0.1x — ~15k-token-equivalent at a 150k context against
the ~190k-equivalent a 1.25x re-write of the same conversation costs. Six check-ins across
a 20-minute test run cost less than one ttl-expiry write.

**Option A, the narrow carve-out, and not option B.** Backgrounding stays refused by
default; it is allowed ONLY for the shapes the new foreground rule refuses. Option B —
backgrounding allowed generally for a headless turn — is rejected: it buys nothing the
carve-out does not (the 28 measured shell writes are all three carved-out shapes) and it
re-opens the orphan class §4 closed, because every OTHER background job is one nobody has
a reason to poll for.

### 3. The foreground refusal (PreToolUse)

**What it does.** Refuses a foreground call whose wall clock is known in advance to
outlive the cache, and tells the caller to background it and poll.

**Where.** New `hooks.long_foreground_decision(payload, env)` in src/jarvis/hooks.py,
beside `background_task_decision`, called from `preflight_decision`
(src/jarvis/hooks.py:1541) in the Bash chain **immediately before**
`background_task_decision` and therefore before the `is_jarvis_command_chain` auto-allow.
That ordering is the enforcement, not a preference: the auto-allow waves through every
`jarvis …` command, so a refusal placed after it passes a unit test and does nothing in
production — the argument the docstring already makes for the PR checks, the summary cap
and `investigator_bash_decision`. It sits after `gate_decision` and
`under_review_decision` for the reason those are first: a narrowed or gated session is the
stricter state.

No-ops unless `env[TURN_TRANSPORT_ENV] == TRANSPORT_HEADLESS`, exactly as
`background_task_decision` does. Under `spawn_background` the notifications arrive, the
gap is the supervisor's problem, and this rule has no subject.

New module constants, spelled and not imported — the module comment at
src/jarvis/hooks.py:373 gives the reason (`import jarvis.claude_cli` costs 56ms against
this module's 27ms, on a hook that runs on every Bash call):

```python
#: The longest a single foreground call may block. Under `inspection.TTL_5M` (300s) with
#: room for the call itself, so a lead polling on this rhythm never loses the cache.
MAX_FOREGROUND_SECONDS = 240
LONG_SEATS = ("jarvis-implementer", "jarvis-spec-writer")
```

#### 3a. Matcher shapes

One shared predicate, `hooks.long_shell_shape(command) -> str | None`, returning the arm
name (`"suite"`, `"ci-watch"`, `"sleep"`) or `None`. It works on
`_mask_shell_text(command)` (src/jarvis/hooks.py:405) so a shape inside a quoted string or
a comment is prose, the same masking the shell half of §4 already depends on.

**`"suite"` — a whole-suite test run.** A `pytest` word in command position, in any of the
three spellings the fleet uses (`pytest`, `uv run pytest`, `python -m pytest`), where no
positional argument contains `::` and not every positional argument ends in `.py`. A
directory argument, or no path at all, is a whole-suite run.

Deliberately NOT matched: `pytest tests/test_hooks.py`, `pytest
tests/test_hooks.py::test_one`, `pytest --collect-only`, `pytest --version`, `pytest -k
expr tests/test_hooks.py`. Each is seconds, and a rule that refused them would be worked
around rather than obeyed — kn-27fed9d2 item 7: a hook in front of Bash is judged on its
false positives.

The deny text for this arm must NOT read as permission to run the suite locally. The
pinned knowledge entry says a worker must not run the whole suite locally at all; the
text names that first ("your targeted tests are the evidence; the panel runs the suite"),
and offers the background-and-poll route second, for a project whose own standing
instructions require a full run before a pull request. A project in that position now has
a legal route instead of an unavoidable $0.56.

**`"ci-watch"` — waiting for CI.** `gh run watch` and `gh pr checks --watch` (the flag in
any position). Deny text leads with the rule the briefing already carries at
src/jarvis/worker_brief.py:500 — *DO NOT WAIT FOR CI EITHER* — because finishing is
cheaper than polling: the OS holds the validation round until GitHub has reported and
nothing is billed while it waits. Background-and-poll is the second-best exit, named
second.

NOT matched: `gh pr checks` without `--watch`, `gh run view`, `gh run list`. Those return
at once.

**`"sleep"` — a wait or a poll loop.** Either of:

- `sleep N` in command position with `N` parsed as seconds (`m`/`h` suffixes multiplied)
  greater than `MAX_FOREGROUND_SECONDS`;
- a `while` or `until` keyword in the masked command with a `sleep` word anywhere after
  it — the loop's wall clock is not bounded by its sleep argument, however small.

NOT matched: `sleep 200` (the sanctioned check-in wait — this threshold is what makes the
rhythm in §5 legal), `sleep 0.5`, a `for` loop containing `sleep` (bounded iterations),
`grep -r sleep src/`, and any command whose first word is `timeout N` with
`N <= MAX_FOREGROUND_SECONDS` — a loop with a bound on it is not an unbounded wait, and
refusing it would leave the lead no way to express "poll, but give up in time".

**`Agent` — a long seat in the foreground.** Separate arm, separate predicate
`hooks.long_seat_call(payload) -> str | None`: `tool_name` in `("Agent", "Task")`,
`tool_input.subagent_type` in `LONG_SEATS`, and `tool_input.run_in_background` falsy.
Deny text: set `run_in_background: true` and collect with `TaskOutput` every 3-4 minutes.

This is the 55-write, $33.5 half of the bill and the larger one. kn-27fed9d2 records that
a foreground `Agent` call IS a join and that `jarvis inspect` already attributes its whole
wall clock as blocked time (`inspection.DELEGATION_TOOLS`, `ToolSpan.is_join`), so the
measurement side needs nothing.

Not matched: every other `subagent_type`, and any seat already backgrounded. `LONG_SEATS`
is the two crew seats §5 of the 2026-09-23 spec ships, because those are the ones whose
median duration in the measured period exceeded the TTL. A short seat blocking for 4
minutes is cheap and refusing it would tax every delegation with a hook process.

#### 3b. The matcher in settings.base.json makes half of this reachable

src/jarvis/assets/settings.base.json:14 reads:

```
"matcher": "Bash|Edit|Write|NotebookEdit|mcp__serena__search_for_pattern|mcp__plugin_serena_serena__search_for_pattern"
```

It does not name `Agent` or `Task`. **Until it does, the foreground-`Agent` refusal is
unreachable in production and only the unit test sees it.** The matcher must become
`Bash|Edit|Write|NotebookEdit|Agent|Task|mcp__serena__…`.

The `$comment` on that entry explains why the list is named exactly and never `mcp__.*`:
each matched call is a ~155ms hook process and Serena's symbol tools are called
constantly. The arithmetic for adding these two is different and it holds: the fleet made
272 foreground delegations in the measured period, so ~155ms x 272 = 42 seconds of hook
process against $33.5 of cache writes. Delegation is rare and expensive; a symbol lookup
is frequent and cheap.

Two follow-ons for the implementer, both cheap to confirm and neither assumed here: the
new matcher reaches a live project only when its settings are rebuilt
(`bootstrap.build_settings`, `dispatch._write_worker_settings` on every turn), and
`bootstrap.settings_drift` is what reports a project still carrying the old one.

**How it is proved.** §8.

### 4. The carve-out in `background_task_decision`

**What it does.** Lets exactly the refused shapes be backgrounded, and refuses everything
else as it does today.

**Where.** `hooks.background_task_decision` (src/jarvis/hooks.py:435), after its existing
transport and `run_in_background`/`backgrounds_through_shell` tests, gains one early
return:

```python
if long_shell_shape(tool_input.get("command", "")):
    return None      # §3 refuses this shape in the FOREGROUND; backgrounding is the fix
```

**One matcher, two call sites, so they cannot drift.** `long_shell_shape` is the single
predicate: §3 refuses the shape when foreground, §4 permits it when backgrounded. A second
spelling of "long" would let a shape be refused in both positions, which leaves a lead
with no legal way to run it at all — the failure mode this arrangement exists to make
impossible. kn-d4d5a967's rule, applied to a matcher instead of a status set.

The `Agent` side needs no carve-out: `background_task_decision` tests
`tool_name != "Bash"` and returns `None`, so a backgrounded seat was never refused.

Still refused, unchanged, with today's text: a backgrounded server, a `nohup` script, a
`setsid` anything, a `&`-terminated command that is not one of the three shapes. Those are
jobs nobody polls for, and the orphan class is theirs.

**How it is proved.** §8.

### 5. The Stop guard

**What it does.** Refuses to end a turn that still has an uncollected background task —
Bash or Agent. This is what makes §4 safe: backgrounding is permitted because ending the
turn on it is not.

**Where.** New `hooks.uncollected_task_turn_block(store, wo_id, payload, env)`, called
from `handle_hook`'s `elif event == "Stop"` branch (src/jarvis/hooks.py:2534), after
`held_request_turn_block` (src/jarvis/hooks.py:1488) — a gate request unargued is the
stricter finding and should be the one the worker reads. Shape copied from that function
verbatim, because the runtime offers exactly one mechanism for holding a session at a turn
boundary:

- returns `{"decision": "block", "reason": …}`, the Stop hook's own shape (no
  `hookSpecificOutput` — `main_hook` at src/jarvis/hooks.py:2564 keys on either);
- `if payload.get("stop_hook_active"): return None` — one continuation, not a loop. A lead
  that ignored the reason once is better parked than spun, and a task that is genuinely
  hung must not make the turn unendable;
- `return None` when the work order's status is in `invariants.TERMINAL_STATUSES`;
- writes a timeline event `background_task_uncollected` with `session_id` and the job ids,
  so the record says why the turn was held.

**Where the evidence comes from: the transcript, through `background.jobs_left_running`
(src/jarvis/background.py:115).** That function already answers exactly this question —
"did this turn collect what it launched" — from the transcript and nothing else, with the
`_scan_calls` / `_scan_results` / `_notified` walk, the `KILL_TOOL`/`POLL_TOOL` matching
and the fail-open `_prose_of` rule that keeps the detector's kill-switch out of the
worker's own writing. Call it with the turn's bounds and a one-entry index built from the
Stop payload:

```python
session_id = str(payload.get("session_id") or "")
path = Path(payload.get("transcript_path") or "")
jobs = background.jobs_left_running(
    session_id, since=store.latest_turn(wo_id)["started_at"], until=time.time(),
    index={session_id: [path]})
```

Rejected: per-call state written by the PostToolUse hook. That hook is unmatched — one
`stat()` on every tool call in every worker session (settings.base.json:22) — so a launch
ledger there means a write per call to buy a fact that is already on disk, and a second
spelling of collection that can disagree with `background.py`'s. The transcript is also
the only source that survives the process: `background.orphaned_in_turn` reads it at reap
and this hook reads it at Stop, one parser, two readers.

The cost asymmetry is what makes this affordable: PreToolUse runs per call and spells its
constants rather than importing (src/jarvis/hooks.py:373), **Stop runs once per turn**, so
`from . import background` there is a 27ms-class import on a boundary that already opens
`ProjectStore` and reads the timeline.

**The predicate, and its bias.** Block only when a launch is recorded in this turn and NO
collection evidence exists for it. Never block on a task proven finished — a `KillShell`,
a poll whose status was anything other than `running`, or a harness
`<task-notification>` all clear it, and `jobs_left_running` already implements each. The
clear case this must catch, and the one the test pins, is a background start with no
collection call at all in the turn. Where the transcript cannot answer — no session id, no
file, an unreadable one — `jobs_left_running` returns empty and the turn ends: a guard that
blocks on missing evidence would trap a worker with no way out.

**Two extensions to `background.py`, and they are the `Agent` half of the user's ruling.**
`_scan_calls` already remembers any `tool_use` carrying `run_in_background`, so a
backgrounded `Agent` is recorded; two things about collecting one are Bash-shaped today:

- `POLL_TOOL = "BashOutput"` becomes a tuple with `TaskOutput` in it, and the id
  parameters read tolerantly — `bash_id`, `shell_id`, plus whatever the task poll names
  its subject. UNVERIFIED against a live `TaskOutput` result, like `RUNNING` before it, so
  the same tolerance the module already documents applies: any `<status>` tag, any
  `status` field, kn-df5574d3.
- `_scan_results` keys a launch on `toolUseResult.backgroundTaskId`. A background `Agent`
  may report its id under another key or none; fall back to the launching `tool_use` id,
  so a launch with no reported id is tracked rather than silently dropped.

**The block text** (the correction first, the reason second, one exit):

> You may not end this turn: background task(s) {labels} were started in it and never
> collected, and ending here kills them — nothing wakes you and nobody reads their output.
> Collect them now, in this turn: `BashOutput`/`TaskOutput` every 3-4 minutes until each
> one reports finished, and `KillShell` anything you no longer want. Each check-in is a
> cache READ; ending the turn and starting another is a full re-write of this
> conversation. If a task is genuinely hung, kill it and say so in your final message.

`background.labels` renders `{labels}`, so the ids the user can check against `ps` are the
ids in the block — the same rendering `RESUME_NOTE` uses.

**The backstop stays.** `worker_session._reap` still calls `background.orphaned_in_turn`
and `background.nudge`. The Stop hook prevents; the reaper catches what a hook cannot see
(a job started inside a script, or a seat's own shell). Prevention plus backstop, the same
arrangement §2 of the 2026-09-23 spec argues for.

**How it is proved.** §8.

### 6. The brief and the standing instructions

**What it does.** Replaces the two places the OS asks for the behaviour that costs the
$52, without losing the true half of what they say.

**Where.** `src/jarvis/worker_brief.py`, both sites, and `TEMPLATE_VERSION` bumped.

**6a. `core_contract`, src/jarvis/worker_brief.py:262.** The bullet keeps its heading
sentence — *A turn is one-shot and NOTHING wakes you when a background job ends* — because
it is still true and is why the rhythm is a poll and not a wait. What changes is the
prescription. It must carry, in the bullet's own compressed voice:

- long work — a whole-suite test run, a CI watcher, a `jarvis-implementer` or
  `jarvis-spec-writer` seat — runs in the BACKGROUND (`run_in_background: true`), and the
  foreground call is REFUSED;
- check in every 3-4 minutes with `BashOutput` / `TaskOutput`, and never let one call
  block longer than 240 seconds: your prompt cache lives 5 minutes, and a call after it
  expires re-sends this whole conversation at the write rate;
- NEVER end the turn with a task uncollected — the Stop hook refuses it, and ending would
  kill the task;
- everything else still must not be backgrounded.

**6b. The standing-instructions section, src/jarvis/worker_brief.py:514,
`# A turn is one-shot`.** Same rewrite at prose length. The sentence that must go is *"So
work that outlasts one command runs in the FOREGROUND and you wait for it"* — that is the
defect, stated as policy. What replaces it is the rhythm, with the reason attached: the
five-minute cache, the 1.25x re-write, the 0.1x read. The three things that start your
next turn, and the warning about ending on "I'll pick this up when the background run
finishes", both stay as they are: nothing about backgrounding changes the fact that no
event re-invokes a worker. The section at src/jarvis/worker_brief.py:495-503 (do not run
the suite, do not wait for CI, kn-356c724b) needs no change and is now the same advice
from the other side.

**6c. `background.RESUME_NOTE`, src/jarvis/background.py:282.** Not in the user's list, and
specified here because it is a third copy of the superseded sentence: *"Re-run it in the
FOREGROUND and wait for it, however long it takes."* It is the text a nudged worker reads
first. Change that one clause to the poll rhythm; keep every other word, including the
"nobody typed this message" attribution.

**How it is proved.** §8.

### 7. Section 4 of the 2026-09-23 spec, amended in place

**What it does.** Stops the older spec from contradicting this one. Two specs disagreeing
about the same hook is how the implementer of the next change picks the wrong rule.

**Where.** `docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md`, §4 (lines
108-142), edited in place — not deleted, and its §1 problem statement (issue #575,
wo-d81fcc15's 62 hours) stands untouched, because that class is still closed.

§4 now reads: backgrounding is refused **by default**; the three shapes named in §3 of
this document are refused in the FOREGROUND instead and must be backgrounded and polled;
the Stop guard is what makes that safe. The deny-text paragraph (lines 127-130) loses "re-
run in the foreground" as the universal correction and gains the split. The listed tests
stay listed. §4 ends with a pointer:
`docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md`.

Its "Rejected alternatives" entry *Reword the briefing again* also stays: §6 here is a
rewording, and it is not the control — §3 and §5 are.

### 8. The knowledge-base entry

Supersedes kn-6dcaf055 (*a rule the OS only asks for in the briefing is not enforced*),
which is not wrong and is not the whole finding any more: the OS enforced a rule that was
itself the defect. Post verbatim, topic `worker-turns`:

> A headless turn must not BLOCK, and that is not the same as must not BACKGROUND. The
> 5-minute prompt cache means any tool call more than 300 seconds after the previous one
> re-sends the whole conversation at the 1.25x write rate — 85 of those happened mid-turn
> across 25 production work orders, 9.1M tokens, ~$52, 8.4% of spend (issue 868), every
> gap under 60 minutes and the median 9.3. So three shapes are refused in the FOREGROUND
> and must be backgrounded and polled: a whole-suite test run, a CI watcher (`gh run
> watch`, `gh pr checks --watch`), and a sleep or poll loop — plus a foreground
> `jarvis-implementer` or `jarvis-spec-writer` call, which was the larger half at $33.5.
> The rhythm is a collect call every 3-4 minutes, each one a 0.1x cache read, and no
> single call blocking past 240 seconds. Everything ELSE still may not be backgrounded,
> and no turn may END with a task uncollected: the Stop hook refuses it, because the task
> dies with the turn (issue #575). kn-6dcaf055's general form survives — a rule only asked
> for is not enforced — with one addition: enforcing a rule at the tool call makes the
> rule's CONTENT load-bearing, so a wrong rule gets obeyed 163 times before anyone counts
> the bill.

### 9. How it is proved

`tests/test_background_refusal.py` (extended — it already owns this hook's coverage and
builds its env from `claude_cli.TURN_TRANSPORT_ENV` / `TRANSPORT_HEADLESS` so the
constants cannot drift):

- `test_foreground_whole_suite_run_denied` — `uv run pytest tests/`, `pytest`,
  `python -m pytest tests/ evals/`.
- `test_targeted_test_run_allowed` — `pytest tests/test_hooks.py`,
  `pytest tests/test_hooks.py::test_one`, `pytest --collect-only`, `pytest -k expr
  tests/test_hooks.py`.
- `test_foreground_ci_watch_denied` — `gh run watch`, `gh pr checks 42 --watch`.
- `test_ci_read_without_watch_allowed` — `gh pr checks 42`, `gh run view 7`,
  `gh run list`.
- `test_long_sleep_and_poll_loops_denied` — `sleep 900`, `sleep 20m`,
  `while true; do sleep 30; gh pr checks; done`, `until gh pr checks; do sleep 60; done`.
- `test_short_sleep_and_bounded_loop_allowed` — `sleep 200` (the check-in wait),
  `sleep 0.5`, `for i in 1 2 3; do sleep 2; done`,
  `timeout 200 bash -c 'while :; do sleep 5; done'`.
- `test_long_shape_inside_quotes_or_comment_allowed` — `grep -r "sleep 900" src/`,
  `echo 'gh run watch'`, `pytest tests/test_x.py  # not the whole suite`.
- `test_suite_deny_text_points_at_the_pinned_rule` — the text names targeted tests before
  it names backgrounding.
- `test_same_shapes_allowed_when_backgrounded` — every denied command above, with
  `run_in_background=True`, returns `None` from `background_task_decision`.
- `test_other_backgrounding_still_denied` — `nohup ./server.sh &`,
  `run_in_background=True` on `./server.sh`; today's deny text unchanged.
- `test_foreground_long_seat_agent_denied` / `test_backgrounded_long_seat_allowed` /
  `test_other_subagent_type_allowed_in_foreground`.
- `test_no_op_without_transport_env` / `test_allowed_when_transport_is_background` — both
  arms, matching the existing pair.
- `test_long_foreground_precedes_the_jarvis_auto_allow` — asserts the position in
  `preflight_decision`, the ordering that makes the rule reachable in production.
- `test_settings_matcher_names_agent` — reads
  `src/jarvis/assets/settings.base.json` and asserts `Agent` is in the PreToolUse matcher.
  Without this the `Agent` arm is dead code, and no behavioural test would say so.

New `tests/test_uncollected_task_block.py` (a Stop-hook guard is a different fixture set —
a store, a turn row and a transcript file — from a pure payload-in/decision-out check):

- `test_turn_with_uncollected_bash_task_blocked` — `decision == "block"`, and the reason
  names the job id.
- `test_turn_with_uncollected_agent_task_blocked` — the user's ruling, on the `Agent` half.
- `test_collected_task_does_not_block` — a `BashOutput` poll reporting anything other than
  `running`; plus a `KillShell`; plus a harness `<task-notification>`.
- `test_stop_hook_active_does_not_block_twice` — one continuation, not a loop.
- `test_terminal_status_does_not_block`.
- `test_missing_transcript_does_not_block` — no session id, no file, unreadable file.
- `test_held_gate_request_block_still_wins`.
- `test_block_writes_a_timeline_event`.

`tests/test_worker_brief.py` (extended): `test_core_contract_carries_the_poll_rhythm` (the
bullet names background, 3-4 minutes, 240 seconds and "uncollected"),
`test_core_contract_no_longer_says_re_run_in_the_foreground`,
`test_standing_instructions_carry_the_carve_out`, `test_template_version_bumped`.

`tests/test_one_shot_turn.py` (extended, where `background.py` is covered):
`test_task_output_poll_collects_a_background_agent`,
`test_launch_without_a_reported_task_id_is_still_tracked`,
`test_resume_note_no_longer_prescribes_the_foreground`.

`tests/test_inspection.py` (extended): `test_polled_turn_has_no_mid_turn_ttl_expiry` — a
transcript fixture where a background task runs 20 minutes and the lead collects every 4
minutes; `_writes` classifies zero `TTL_EXPIRY` inside the turn, and `raise_alarms`
produces no `JOIN_ALARM`. Its negative twin, a fixture with the same 20 minutes as one
foreground join, keeps producing both — that pair is the measurement of the fix, and
without the negative the positive proves only that the fixture is short.

## Out of scope

- **Compaction mid-turn.** No mechanism, and the polling rhythm removes the need.
- **The 1-hour cache**, per project or fleet-wide. Rejected above on its arithmetic.
- **An alarm threshold change.** `inspect.alarm_join_seconds` and the `JOIN_ALARM` text
  are correct and stay; a lead obeying §3 stops reaching them.
- **`prefix-miss` writes.** A different defect with a different fix (kn-335170a1),
  upstream of the clock.
- **Every other `subagent_type`.** `LONG_SEATS` is two names. Widening it is a measurement
  question for the next period, not a guess for this one.
