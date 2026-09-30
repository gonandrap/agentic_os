# Harvesting a dead turn

wo-2ae960c4 · issue #888 · 2026-09-30

## The problem

A worker turn whose `claude` process exits without writing its result JSON loses
everything the turn did. `worker_session._reap` (`src/jarvis/worker_session.py:809-834`,
the `if result is None:` branch around `:821`) records one string and settles:

```python
error = (_stderr_tail(turn) or _transcript_error(store, wo_id, turn) or NO_RESULT)
payload = {"seq": turn["seq"], "error": error[:500]}
store.add_event(wo_id, "turn_paused" if auth else "turn_failed", payload)
...
return store.finish_turn(turn["id"], "failed", error=error, cost_usd=cost, ...)
```

`NO_RESULT` is `"the turn's process ended without writing a result"`
(`worker_session.py:944`). `Daemon.settle_work_order` then takes the work order to
`failed` (`mem:work-order-lifecycle`, settlement table), and the record holds one
sentence of transport diagnosis and no statement about the WORK.

The work exists on disk and nothing reads it:

| evidence | where it already lives | who reads it today |
|---|---|---|
| last assistant message | `Stop` hook events / `usage.said_in_session` | nobody on this branch — `_last_assistant_message` (`:925`) is called only on the success path (`:914`) |
| commits + uncommitted files | the worktree | `landing.authored` (`landing.py:290`), called from `ops.authorship` at FINISH, never at a failed reap |
| pushed branch, pull request | the worktree's upstream, `work_orders.pr_url` | `Daemon.poll_pull_requests`, only for `waiting_pr_merge` |
| background jobs left running | `background.orphaned_in_turn` (`background.py:98`) | called at `worker_session.py:917` on the SUCCESS path only |

Consequence: `jarvis wo retry` relaunches with `ops.RETRY_NOTE` (`ops.py:1191`), which
asks the worker "Say where you got to" — the OS making the worker re-derive, from its own
transcript, facts the OS could have read off disk in three `git` calls. Uncommitted edits
are the tail `landing.Authored`'s docstring already names as lost once (issue #232, 172
worktrees deleted in a disk sweep). A worktree reclaimed before anyone retries takes them
with it, and the transcript is pruned by Claude Code on its own schedule
(`autopsy.py:1-7`).

Root cause, named: the no-result branch is the one settlement path in the OS that writes
a diagnosis instead of a reading. Every other settlement (`ops.finish` →
`Authored.record()`, the success reap → `background.record`) writes down what it saw
while it could still see it. This one does not.

## The fix

On that branch only, the daemon harvests a partial result — mechanically, with no model
call — into a `turn_harvested` timeline event, and makes a local WIP checkpoint commit so
uncommitted edits outlive the worktree.

### Settled, not reopened

1. **`work_orders.result_summary` is NOT written** (Neo q1131). It is the worker's own
   delivered prose, fed verbatim to the panel (`evidence.py:537`), Neo autoreview
   (`autoreview.py:1289`, `:1504`), search (`search.py:91`) and read as a lifetime marker
   by the settler (`invariants.py:420`). Machine prose there makes a failed order read as
   delivered.
2. **Scope: the `turn_failed` outcome of the no-result branch, nothing else** (Neo
   q1132). NOT the `PAUSE_AUTH` pause inside that same branch, NOT `usage_limit` /
   `transient` / `budget` — those resume the same session, and a checkpoint commit under
   a live worker surprises it mid-task.
3. **The checkpoint is a LOCAL commit** (Neo q1132). Never a push, never a new branch,
   never a pull request. The push/PR half of the harvest is read-only reporting of what
   the worker already pushed.

### 1. Where the code lives

New module `src/jarvis/harvest.py`. Not `worker_session`:

- `worker_session` is ~1400 lines and is the conversation layer — it knows processes and
  sessions, not repositories.
- The harvest needs `landing`, and `landing.py:104` does `from . import worker_session`.
  A module-level import back would be a cycle.

`harvest` imports `landing`, `background`, `branchproof`, `db` at module level;
`worker_session._reap` imports it LAZILY inside the branch (`from . import harvest`),
which is that module's existing idiom (`:415`, `:930`, `:1353`). Layering: adapter, below
`ops`/`daemon`, above the stores.

### 2. What is read, and from what

Entry point:

```python
def collect(store, wo, turn, *, said: str) -> dict[str, Any]   # the payload
def write(store, wo, turn, *, said: str) -> dict | None         # collect + add_event
def of_turn(store, wo) -> dict | None                           # read it back
def retry_brief(store, wo) -> str                               # "" when none
```

`said` is passed IN, not read here: `worker_session._last_assistant_message(store, wo_id,
turn)` (`:925`) already walks the `hook:Stop` events, and `_transcript_error` (`:976`)
already walks `usage.said_in_session`. `_reap` calls the first and hands the result over,
so harvest never imports `worker_session`. Clipped to 2000 chars in the payload.

Worktree: `landing.worktree_of(store.project_path, wo)` (`landing.py:278`) — the
one-liner that exists precisely so callers stop assembling `worker_session.worktree_path`'s
`ProjectSpec` shim. `store.project_path` is set at `project_store.py:1628`;
`ops.authorship` (`ops.py:4779`) is the precedent for this exact pair.

Order-level authorship: `landing.authored(worktree)` → `Authored(branch, base, commits,
dirty, unreadable)`, serialised with `Authored.record()` (`landing.py:213`). Three git
calls, never raises, `unreadable` carries the reason. `base` comes from
`evidence.base_ref` (`evidence.py:722`) — the pinned merge-base ladder, and the reason
this spec does not invent a fourth guess at the default branch.

Turn-level scoping: `landing.authored` measures the whole ORDER, not "since this turn
started". So `worker_session._launch` (`:726`) gains one field on the `turn_started`
event payload — `"head": <sha at launch>` — read with one `git rev-parse HEAD` in `cwd`.
Harvest then reports `rev-list <head>..HEAD`. When the field is absent (turns launched
before this ships, or git unreadable), the payload records `"since": "order"` and falls
back to `base..HEAD`; when present, `"since": "turn"`. A reader is never left guessing
which question was answered.

Pushed state, read-only: `git rev-parse --abbrev-ref HEAD@{upstream}` and
`rev-list --count @{upstream}..HEAD`. Pull request: `wo["pr_url"]` first, then
`landing.pr_urls_in` (`landing.py:162`) over the harvested `said`. No network — `gh` is
`Daemon.poll_pull_requests`' business and it runs on its own cadence.

Background jobs: **harvest calls `background.orphaned_in_turn` and `background.record`;
the success-path call at `worker_session.py:917` stays exactly where it is.** Reason:
`background.record` (`background.py:277`) is the ONE writer of the `background_orphaned`
event that `background.pending_orphan`, `background.resume_note` and
`timeline.VOID_MESSAGE` all read, and it is already keyed on the latest turn. Copying the
job list into the harvest payload would give the OS two answers to one question. Harvest
passes `msg_id=None` — a failed turn recorded no reply, which that function's docstring
already provides for — and the harvest payload carries only `"jobs": <count>` so the
"nothing to harvest" test can see it. Renderers read the names through
`background.jobs_of(background.pending_orphan(store, wo))`.

Git runner: `branchproof._git` is promoted to `branchproof.run(repo, *args, stdin=None)`,
with `_git = run` kept as the in-module alias so no existing call site changes. It is the
only one of the three copies with a **timeout** (`GIT_TIMEOUT = 30`, `branchproof.py:36`)
and a non-interactive environment (`_env()`, `:47` — `GIT_TERMINAL_PROMPT=0`,
`GIT_ASKPASS=true`), both of which this path needs: it runs on the daemon tick.
`landing._git` (`:483`) has neither and stays read-only where it is. A fourth copy of the
`subprocess.run` block is what this avoids.

### 3. The checkpoint commit

Only when `Authored.dirty` is non-empty and no refusal below applies.

```
git -c user.name=Jarvis -c user.email=jarvis@localhost \
    -C <worktree> add -A
git -c ... -C <worktree> commit --no-verify --no-gpg-sign -m "<message>"
```

`--no-verify`: a project's pre-commit hook can reject or hang, and the daemon is not the
place to run a project's test suite. `add -A` stages untracked files — `Authored`'s own
rule, `--untracked-files=all`, for the worker that wrote a new package and never staged
it; `.gitignore` still applies.

Message:

```
WIP: Jarvis checkpoint of <wo-id> turn <seq>

This turn's process ended without writing a result. The OS committed what was in
the worktree so it is not lost. Nobody reviewed it. Amend, reset or rewrite freely.

Jarvis-Checkpoint: <wo-id>/<seq>
```

The trailer is the machine handle: a later harvest skips a HEAD that already carries it,
and the relaunched worker can find its own checkpoint with `git log --grep`.

**Not a stash.** CLAUDE.md's own environment note: the stash stack is shared across every
worktree and another session may pop it. A stash is the one mechanism here that CAN
collide; a commit inside one worktree cannot.

### 4. Can it collide with the worker's next turn?

No, on three counts, and only the first is a design choice:

1. Harvest runs on the `turn_failed` outcome, which settles the work order to `failed`.
   A `failed` order gets no turn until `jarvis wo retry` or `jarvis wo send` queues a
   message, which the daemon delivers on a LATER tick.
2. `_reap` is reached only after `claude_cli.process_alive(turn["pid"])` is false and
   `_unit_still_running(turn)` is false (`worker_session.py:785-790`). The process is
   gone.
3. `poll()` is single-threaded inside `Daemon.settle_turns`; one turn at a time per work
   order (`worker_session.busy`).

The residual risk is an ORPHANED BACKGROUND JOB still writing files while `git add -A`
runs. Accepted: the commit then captures a half-written file, which is strictly better
than losing it, the job list is on the record beside it via `background.record`, and the
commit message says nobody reviewed it.

### 5. Persistence and the readers

One `turn_harvested` event on `wo_events`, payload version 1:

```json
{"seq": 7, "version": 1, "empty": false, "since": "turn",
 "said": "…", "authored": {"branch": "wo-2ae…", "base": "origin/main",
 "commits": 3, "dirty": ["src/jarvis/harvest.py"]},
 "turn_commits": ["a1b2c3d"], "checkpoint": "9f8e7d6", "checkpoint_skipped": "",
 "detached": false, "upstream": "origin/wo-2ae…", "unpushed": 1,
 "pr_url": "", "jobs": 0, "unreadable": ""}
```

Durability: `wo_events` is SQLite in `<project>/.jarvis/jarvis.db` and outlives both the
transcript and the worktree. **No new column and no migration** — `background.record` is
the precedent (a derived reading written at settle and rendered into a later relaunch
note), and `autopsy.py:19-24`'s rule is the reason it is not sealed onto the row: seal
what EXPIRES. The git facts here are already frozen values, not a live reading.

`harvest.of_turn(store, wo)` reads it back keyed on `store.latest_turn(wo["id"])["seq"]`,
which is `background.pending_orphan` (`background.py:291`) copied deliberately: keyed on
the turn, so nothing has to be cleared once the retry has run.

Three readers:

1. **`jarvis wo show`** — `cli.py:2974-3048` gains
   `**({"harvest": h} if (h := ops.harvest_state(store, wo)) else {})`, on that dict's
   stated never-always rule (absent when there is none), plus `_readable_harvest` in the
   render chain at `cli.py:3052`.
2. **Dashboard work-order page** — `ui/app.py:1310-1331` passes
   `harvest=ops.harvest_state(store, wo)`, rendered in `work_order.html` immediately
   above the retry control (`retry=ops.retry_state`, `app.py:1274`): it is what the user
   reads before pressing the button.
3. **Timeline** — `turn_harvested` is a SIGNAL kind (NOT added to `timeline.DEBUG_KINDS`,
   `timeline.py:32`); one `_describe` branch beside `"turn_failed"` (`timeline.py:225`):
   `("Harvested what the turn left behind", "3 commits, 1 uncommitted file checkpointed
   as 9f8e7d6")`, or `("Nothing to harvest", "the worktree was clean and the turn said
   nothing")`.

`ops.harvest_state(store, wo)` is the single render contract, `ops.retry_state`'s shape
and its `None` convention.

### 6. The relaunch brief

`ops.retry` (`ops.py:1255`) line 1277 becomes:

```python
text = message if message is not None else (harvest.retry_brief(store, wo) or RETRY_NOTE)
```

`RETRY_NOTE` stays as the fallback and is unchanged. `authored` stays
`bool(message)` — the brief is an OS literal either way, so §3's rule ("an OS literal
must never carry the user's stamp") still holds.

`retry_brief` renders the payload as prose the worker can act on:

```
[Jarvis] The OS is relaunching this work order because the user asked for it. Its
last turn ended without writing a result. Here is what the OS found on disk, so you
do not have to re-derive it:
- you last said: "…"
- `wo-2ae…` carries 3 commits over `origin/main`, 1 of them made in that turn
- 1 uncommitted file was committed for you as a WIP checkpoint 9f8e7d6 (trailer
  `Jarvis-Checkpoint: wo-2ae…/7`) — amend, reset or rewrite it freely
- 1 background job died with the turn: …
Verify this against the worktree, then carry on. Do not start again.
```

**`worker_session._nudge` (`:1361`) is NOT changed, and that is the point.** `_nudge`
speaks to a turn relaunched after a PAUSE — usage limit, transient, budget, auth — and
decision 2 excludes every one of those from the harvest. There is never a harvest for a
turn `_nudge` addresses, so a harvest-aware branch there would be dead code that reads as
a promise.

### 7. Failure modes

Every one records rather than raising. `unreadable` and `checkpoint_skipped` carry the
reason verbatim; the event is written in all cases.

| case | what happens |
|---|---|
| no worktree on disk | `landing.authored(None)` → `unreadable="no worktree on disk"` (`landing.py:299`). `said` and the jobs are still harvested. |
| no session / no transcript | `said=""` — `_last_assistant_message` returns `""` (`:939`), `orphaned_in_turn` returns `[]` on a missing session id (`background.py:110`). |
| no default branch | `Authored(unreadable="no default branch to compare against")` (`landing.py:305`), rung 4 of the ladder. Checkpoint still made: it does not need a base. |
| **nothing changed at all** | `"empty": true` and the event IS written. The timeline says "Nothing to harvest"; `retry_brief` returns `""` so `ops.retry` falls back to `RETRY_NOTE`. Never an empty claim that something was saved. |
| rebase / merge / cherry-pick in progress | detected by `git rev-parse --git-path rebase-merge\|rebase-apply` existing, or `MERGE_HEAD`/`CHERRY_PICK_HEAD`. NO checkpoint; `checkpoint_skipped="a rebase or merge is in progress"`. The read-only half still runs. |
| detached HEAD | checkpoint IS made — the sha is recorded and the reflog keeps it reachable — and `"detached": true` so the reader knows it is on no branch. |
| a git command fails or hangs | `branchproof.run` returns `None` on a non-zero exit, `OSError` or `TimeoutExpired` (30s), and logs. The field becomes `""`/0 and `unreadable` says so. |
| the checkpoint commit itself fails | `checkpoint=""`, `checkpoint_skipped=<git's stderr, clipped>`. The rest of the harvest stands. |
| **harvest raises anything** | `_reap` wraps the ENTIRE call in `try/except Exception: log.warning(...)`. Placement matters: the call goes AFTER `store.finish_turn(...)` and before the return, so a daemon killed mid-harvest leaves a settled turn with no harvest — never an unsettled turn. The `except` is in `_reap`, not inside `harvest`, so an ImportError is caught too. |

## Rejected alternatives

- **Write `result_summary`.** Neo q1131, above. It would make a failed order read as
  delivered to four independent consumers.
- **Ask a model to summarise the transcript.** No model call is permitted on this path —
  it runs inside the daemon tick for every failed turn, and a settlement that depends on
  the network cannot be the thing that records why the network broke.
- **Push the branch / open a PR.** Neo q1132. The OS would be publishing work no one
  reviewed under the worker's branch name.
- **Stash instead of commit.** The stash stack is shared across worktrees; see §3.
- **Harvest in `Daemon.settle_work_order`.** It re-derives from the latest turn on EVERY
  tick, so the git calls and the commit would repeat for as long as the order stays
  `failed`. `worker_session.py:915-918` states the rule: a turn is reaped exactly once
  (kn-089de524).
- **Put it in `worker_session`.** Import cycle via `landing` (§1), and the module is
  already the largest in the package.
- **A `harvest_json` column, or an `autopsy`-style seal.** Needs a migration and a second
  writer for a payload that is already frozen. `wo_events` is durable and
  `background.record` is the standing precedent.
- **Move `background.orphaned_in_turn` out of the success path into harvest.** It would
  change success-path behaviour to buy nothing; the failed path simply calls the same
  function.

## Tests — `tests/test_turn_harvest.py`

1. `test_harvest_records_last_message_and_commits` — fake CLI `turns_fail("silent")`, a
   `git` project with a commit made in the turn; assert the `turn_harvested` payload.
2. `test_harvest_checkpoints_uncommitted_work` — dirty worktree; assert `checkpoint` is a
   sha, `git log -1` carries the `Jarvis-Checkpoint:` trailer, and `git status --porcelain`
   is empty afterwards.
3. `test_checkpoint_is_not_pushed_and_makes_no_branch` — assert `@{upstream}` is unchanged
   and `git branch --list` count is unchanged.
4. `test_nothing_to_harvest_is_recorded_as_such` — clean worktree, silent turn, no reply:
   event written with `"empty": true`, timeline says "Nothing to harvest",
   `retry_brief` returns `""`.
5. `test_harvest_raises_turn_still_settles` — monkeypatch `harvest.write` to raise; assert
   the turn row is `failed`, the work order settles to `failed`, and no exception escapes
   `poll()`.
6. `test_no_harvest_on_pause_paths` — parametrised over usage limit, transient API 500,
   budget exhaustion and auth failure: no `turn_harvested` event, no new commit.
7. `test_harvest_skips_checkpoint_during_rebase` — write `.git/MERGE_HEAD`; assert
   `checkpoint == ""` and `checkpoint_skipped` names the reason, and the read-only fields
   are still populated.
8. `test_no_worktree_records_unreadable` — delete the worktree before the reap.
9. `test_harvest_does_not_write_result_summary` — pin Neo q1131: `result_summary` stays
   NULL after the reap.
10. `test_retry_brief_replaces_retry_note` / `test_retry_note_when_no_harvest` — over
    `ops.retry`, asserting the queued message body.
11. `test_background_jobs_recorded_once` — a failed turn that left a job running writes
    exactly one `background_orphaned` event, and `background.resume_note` still fires.
12. `tests/test_timeline.py::test_turn_harvested_is_signal` — `event_level` returns
    `"signal"` and `_describe` renders both the full and the empty payload.

## Not covered

- Any harvest for a PAUSED turn (decision 2). If a pause later proves to need one, it is
  a different mechanism: the conversation is intact, so the question is what to TELL the
  worker, not what to save.
- Reclaiming or garbage-collecting checkpoint commits. They sit in the worktree until it
  is reclaimed, exactly as the worker's own commits do.
- Feature-order rollup: a failed child's harvest is on the child's record only.
- `jarvis search` over harvest payloads.

## Open

- Clip lengths (`said` 2000, `dirty` list) are asserted but not measured; the first real
  harvest on a large worktree should be checked against `wo_events` row size.
- `git rev-parse HEAD` at `_launch` adds one subprocess per turn (~30ms). Cheap beside
  spawning `claude`, but it is a new cost on the hot path and worth confirming under
  fan-out.
