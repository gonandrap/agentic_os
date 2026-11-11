# Artifact report revive probe: raw evidence (wo-d614af29)

Spec: `docs/superpowers/specs/2026-11-10-artifact-report-revive-spike.md` §2. Script:
`scripts/spike_artifact_revive.py` (throwaway). All data synthetic. Timestamps UTC.

## Setup

| Item | Value |
|---|---|
| CLI | `2.1.322 (Claude Code)` |
| Auth | `claude auth status`: `"authMethod": "claude.ai"`, `"subscriptionType": "max"`: subscription OAuth, no API key |
| Inner kind | synthetic order `{"id": "wo-spikeart", "kind": "researcher", "worktree": <this worktree>}` |
| Briefing | `worker_session.briefing_for(cat.project("jarvis_os"), wo)`, catalog `/home/gonzalo/workspace/production/config/catalog.json` (read only) |
| Permission mode | `auto`, from the briefing; production `jarvis config show`: `os.defaults.permission_mode = "auto"` |
| Model | `claude-opus-5-5` (briefing default) |
| Settings | `/home/gonzalo/workspace/agentic_os/.jarvis/worker-settings/wo-spikeart.json` (kept until revive done) |
| Spawn | `claude_cli.spawn_bg_turn(PUBLISH, cwd, "[spike] artifact revive", **briefing)`, no `--worktree` |
| cwd (spawn and revive) | `/home/gonzalo/workspace/agentic_os/.claude/worktrees/wo-d614af29` |
| Inner session | short `1ff52f19`, sid `1ff52f19-26c4-4755-9aed-acda2c49f619` |
| Transcript | `~/.claude/projects/-home-gonzalo-workspace-agentic-os--claude-worktrees-wo-d614af29/1ff52f19-26c4-4755-9aed-acda2c49f619.jsonl` |

### Env isolation (trap 1)

Script scrubs every `JARVIS_*` and the outer session's `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_CODE_MESSAGING_*`, `CLAUDECODE` etc. from `os.environ`, then sets
`JARVIS_WO_ID=wo-spikeart`, `JARVIS_HOME=/tmp/jarvis-spikeart-home`, before
`briefing_for` runs. So `_write_worker_settings` writes both into settings `env`.

- Settings env, script output: `settings_env: {"JARVIS_WO_ID": "wo-spikeart", "JARVIS_HOME": "/tmp/jarvis-spikeart-home", "JARVIS_WO_KIND": "researcher"}`
- Inner session's own Bash `printenv JARVIS_WO_ID JARVIS_HOME`, tool result 14:46:03.841Z: `wo-spikeart` / `/tmp/jarvis-spikeart-home`
- Inner process `tr '\0' '\n' < /proc/635545/environ | grep -E '^JARVIS_(WO_ID|HOME)='`: empty output. The `--bg` session process carries neither var in its own env; they reach tools and hooks only through settings `env`.
- Hooks with the synthetic id: `hooks.handle_hook` looks `wo-spikeart` up in the project store, finds nothing and no session binding, returns `None` (no-op, no event written). PreToolUse ran the researcher fences (`researcher_write_decision` allows `.jarvis/scratch/`): the Write to `.jarvis/scratch/spikeart-report.html` passed.

## Q1: does a researcher-kind bg session publish a private artifact? YES, under `auto`

Command: `uv run python scripts/spike_artifact_revive.py publish`

- `2026-11-10T14:46:20.819Z TOOL_USE Artifact {"file_path": ".../.jarvis/scratch/spikeart-report.html", "title": "Synthetic decision report", "icon": "report", ...}`
- `2026-11-10T14:46:22.262Z TOOL_RESULT Published ... at https://claude.ai/artifact/JU754gmR89snAzsXVVZyim (Version 1, version id 1794321981-b64b)`
- Inner final reply: `PERMISSION none: no tool asked for permission or was refused`
- No allow rule added. Settings `permissions.allow` holds no `Artifact` rule (worktree Edit/Write, Read, `Bash(jarvis *)`, Serena only); `auto` let the publish through.
- Roster went `busy` to `idle`/`done` with no hang: `roster: {..."pid": 635545, "status": "idle", "state": "done"...}`
- Publish turn, `window_envelope(sid, publish_started, settled)`: `{"input": 8, "cache_read": 174571, "cache_write": 27675, "output": 2586, "total_cost_usd": 0.308066, "api_calls": 4}`
- Watch after publish (verbatim, `ArtifactComments` watch, no url): `- https://claude.ai/artifact/JU754gmR89snAzsXVVZyim — connected, armed by a publish, auto-replies armed, since 2026-11-10T14:46:22.082Z`

Artifact URL (private, synthetic, NOT deleted): https://claude.ai/artifact/JU754gmR89snAzsXVVZyim

## Removal (step 2)

Command: `uv run python scripts/spike_artifact_revive.py remove`

```
stop_bg: true
pid_running_after_stop: false
remove_bg_report: {"report": "removed 1ff52f19", "refusal": ""}
short_absent_from_roster: true
removed_at: 2026-11-10T06:46:45-0800
```

Removal: **2026-11-10T14:46:45Z**. Watch dies with the process; no session holds it from here.

## Park on the user (step 3)

`jarvis wo finish --needs-user` (with or without `--pr`) landed the order in
`validating`, not parked: `status: validating`, `needs_attention: 0` (bug wo-733e6ae2).
Per Neo question 4201 the step was raised in the turn's final message; a push
notification was refused: `Not sent — this terminal is active`.

What the user saw on each comment sent to Claude, with no live session (user's words):
`Comment posted, but a conversation with Claude couldn't start here.Open in Claude`.
Two threads were made, both sent to Claude.

Why (inferred, not measured): Send to Claude hands the comment to a live session whose
watch on the artifact says `auto-replies armed`. The only such session (`1ff52f19`,
watch `connected, armed by a publish, auto-replies armed` at 14:46:22Z) had been
removed at 14:46:45Z, so none existed at 15:12. The comment still posted and the thread
was still activated: the revive replied and resolved it. The counterfactual (a comment
while a session holds the watch) was deliberately not run, to keep Q2 clean.

Consequence: under remove-then-revive, every Send to Claude shows this message, because
no session holds a watch between orders. **User requirement (2026-11-10): the message is
misleading and must be avoided.** Measured paths do not avoid it: a live watching session
is ruled out by the spike's own rules and costs a resident process; a plain comment (no
Send to Claude) is not activated, and the tool refuses reply and resolve on such a
thread. What "Open in Claude" does was not measured.

## Timeline (UTC, in required order)

| Step | Time | Source |
|---|---|---|
| Removal | 2026-11-10T14:46:45Z | `remove` output `removed_at: 2026-11-10T06:46:45-0800` |
| User comments (2) | 2026-11-10T15:12 (both) | thread data: `[the user (owner), sent to you — 2026-11-10T15:12]` |
| Poll 1 (haiku) | 2026-11-10T15:14:11Z | `poll_at: 2026-11-10T07:14:11-0800` |
| Poll 2 (default model) | 2026-11-10T15:14:31Z | `poll_at: 2026-11-10T07:14:31-0800` |
| Revive | 2026-11-10T15:14:48Z | `revive_at: 2026-11-10T07:14:48-0800` |

## Q3: can a `claude -p` poll read the threads? NO

Command: `uv run python scripts/spike_artifact_revive.py poll` (calls
`claude_cli.run_headless_result(READ_ONLY, model=..., cwd, permission_mode="auto")`;
prompt: ToolSearch `select:ArtifactComments`, then `read` the URL).

- haiku: `poll_usage: {"input": 6, "cache_read": 82255, "cache_write": 32592, "output": 920, "total_cost_usd": 0.00535715, "latency_ms": 12133, "model": "claude-haiku-5-5"}`
  Text: `The \`ArtifactComments\` tool isn't available in this session. ToolSearch returned no match for \`select:ArtifactComments\` or for a keyword search.`
- CLI default model (`SPIKE_POLL_MODEL= ... poll`), to rule out the model: `poll_usage: {"input": 4, "cache_read": 45521, "cache_write": 23715, "output": 227, "total_cost_usd": 0.1322352, "latency_ms": 10024, "model": "claude-opus-5-5"}`
  Text: `I couldn't read the comments: no ArtifactComments tool exists in this session. ToolSearch for "select:ArtifactComments" returned "No matching deferred tools found"`
- Same result on the post-revive verify `-p` read (haiku, $0.004680735): `` `ArtifactComments` isn't available in this session ``.
- 3 of 3 print sessions: the tool is absent. The thread was never seen, so the price per poll is moot: a daemon cannot poll this way on CLI 2.1.322. Each print session did offer a "Claude Docs `read` tool" instead; not tried (out of the spec's poll definition), unmeasured.
- Non-LLM signal, **desk research**:
  - `ArtifactComments` tool description (CLI 2.1.322, as loaded in this session): "A comment on a watched artifact that is sent to Claude wakes this session, but only while that artifact's row in that listing says auto-replies armed" and "Only a main-loop session (interactive, SDK, or background) holds a watch, not a subagent, teammate, or print session." So only a live session gets the wake-up; no hook event for it is documented there.
  - Web search `claude.ai artifact comments notification owner email "Send to Claude"`: no claude.ai doc of an owner notification (email, push) on a comment. Nearest: https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them (no comment-notification text), https://claude.com/docs/claude-science/comments (a different app).
  - Not found: any claude.ai-side signal a daemon could watch without an LLM session.

## Q2: does a bare resume of the removed session answer the threads? YES

Command: `uv run python scripts/spike_artifact_revive.py revive` (calls
`claude_cli.resume_bg_turn(sid, REVIVE, cwd)`, cwd identical to publish).

- `same_session_id: true`; `resumed: {"id": "1ff52f19", "session_id": "1ff52f19-26c4-4755-9aed-acda2c49f619", "pid": 796756, ...}`: continued, not forked.
- Read: `TOOL_RESULT 2 comment threads (2 open, 2 activated for Claude, 2 with comments sent to you)`
- Reply: `Replied to comment thread d8614ea6-ac72-40e9-af0f-0611ec905f30 (comment 1be2f05f-af7b-4dc5-a2b2-728497335431)` and `Replied to comment thread 27da8498-8ead-4cc6-8ea4-cf7d2ede09ad (comment 79c02bd9-bb80-4754-99ce-3da050ced921)`
- Resolve: `Marked comment thread d8614ea6-... resolved.` and `Marked comment thread 27da8498-... resolved.`
- Session's own summary: `READ ok d8614ea6-ac72-40e9-af0f-0611ec905f30 27da8498-8ead-4cc6-8ea4-cf7d2ede09ad` / `REPLY ok` / `RESOLVE ok` (not counted as proof; see below).
- Inner process env after resume: `inner_proc_env: {"JARVIS_WO_ID": "<unset>", "JARVIS_HOME": "<unset>"}` (bare resume passes no `--settings`; settings file kept on disk regardless).

### Re-armed watch (verbatim, revived session's `ArtifactComments` watch, no url)

```
- https://claude.ai/artifact/JU754gmR89snAzsXVVZyim — connected, re-armed when this session was resumed, since 2026-11-10T15:14:50.640Z; its comment count is not refreshed yet — action "comments" shows them
```

The bare `--bg --resume` re-armed the watch 2 s after resume. It died with the stop below.

### Independent check

The script's post-revive `-p` read failed (Q3: no tool in print sessions). The check was
instead an `ArtifactComments` `read` from the outer worker session (wo-d614af29, never
the inner session's process), after the inner session was stopped and removed. Verbatim:

```
2 comment threads (0 open, 2 activated for Claude, 2 with comments sent to you).
Thread d8614ea6-ac72-40e9-af0f-0611ec905f30
  resolved (by Claude) · Claude: activated · created 2026-11-10
  [the user (owner), sent to you — 2026-11-10T15:12]
      dbdbfdee| what is this?
  [Claude (via the user) — 2026-11-10T15:15]
      dbdbfdee| Option A: replace the LRU cache with W-TinyLFU, a frequency-based policy (an LFU variant) that admits and keeps keys by how often they are used. In this synthetic report it gives +12 pt hit rate and resists scans, for +6% memory and about 2 engineer-days.
Thread 27da8498-8ead-4cc6-8ea4-cf7d2ede09ad
  resolved (by Claude) · Claude: activated · created 2026-11-10
  [the user (owner), sent to you — 2026-11-10T15:12]
      dbdbfdee| what is the meaning of LRU?
  [Claude (via the user) — 2026-11-10T15:15]
      dbdbfdee| LRU means Least Recently Used: when the cache is full it evicts the key that was accessed longest ago. That is why a big batch scan, which touches many keys once, pushes the hot keys out.
```

(location/anchor rows and the tool's header text trimmed.) The outer session's watch
listing after that read: `No artifact watches in this session.`

## Q4: what does the revive turn cost?

`usage.window_envelope(sid, since, until, schema=claude_cli.USAGE_SCHEMA_VERSION)`:

- `window: {"since": 1794323688.2412255, "until": 1794323715.8971324}` = 15:14:48.24Z to 15:15:15.90Z.
- `revive_envelope: {"input": 8, "cache_read": 253176, "cache_write": 16780, "output": 1162, "total_cost_usd": 0.208147, "api_calls": 4, "cost_by_model": {"claude-opus-5-5": 0.208147}}`
- Why only the revive turn: `since` is taken just before `resume_bg_turn`; `until` just after the roster shows `idle`/`done`. The only earlier turn (publish) ended 14:46:27Z, and the session had no process from 14:46:45Z (removal) to the resume. Its first revive call is 15:14:53Z and its last text 15:15:12Z, both inside the window. No subagent ran.
- For comparison, publish turn: $0.308066, 4 API calls.

## Cleanup

```
stop_bg: true
remove_bg_report: ["removed 1ff52f19", ""]
final_roster_ids: ["19820af1", "1f517f30", "267c11ef", "29d2ce20", "332f5894", "41102ffa", "5e854de3", "62ab0ff3", "75eb4038", "862cc8ad", "b92c3a62", "e79b0407", "f27f0b4d", "f930ee0a", "ff69a075"]
inner_short_absent: true
```

`1ff52f19` absent. No other session stopped or removed. Settings file
`/home/gonzalo/workspace/agentic_os/.jarvis/worker-settings/wo-spikeart.json` left on
disk (gitignored). Artifact not deleted (needs user confirmation).

## Spend of this probe (inner sessions, not booked to wo-d614af29)

publish $0.308066 + poll $0.00535715 + poll $0.1322352 + revive $0.208147 + verify
$0.004680735 = $0.658486 total.
