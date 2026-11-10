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

## Q2-Q4

Pending the user's comment. Park path: `jarvis wo finish --needs-user` (with or without
`--pr`) landed the order in `validating`, not parked (bug wo-733e6ae2); per Neo
question 4201 the step was raised to the user in the turn's final message (a push
notification was refused: `Not sent — this terminal is active`).
