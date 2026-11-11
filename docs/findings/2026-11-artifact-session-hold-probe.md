# Artifact session hold probe: raw evidence (wo-f770f7f0)

Spec: `docs/superpowers/specs/2026-11-10-artifact-session-hold.md` §2. Script:
`scripts/spike_artifact_hold.py` (throwaway). All data synthetic, artifacts private.
Times UTC unless marked PST (`-0800`, script output).

IN PROGRESS: user steps pending (P1 +1 h/+6 h/+24 h, P2, P4 wake, P5 confirm).

## Setup

| Item | Value |
|---|---|
| CLI | `2.1.322 (Claude Code)` |
| Spawn | `claude_cli.spawn_bg_turn(prompt, ROOT, "[spike] hold <role>", **worker_session.briefing_for(cat.project("jarvis_os"), wo))`, catalog `/home/gonzalo/workspace/production/config/catalog.json` (read only), kind `researcher`, mode `auto`, model `claude-opus-5-5` |
| ROOT (cwd) | `/home/gonzalo/workspace/agentic_os/.jarvis/spikehold-proj` (gitignored; `/tmp` refused: `Workspace not trusted`) |
| Store | throwaway `ProjectStore(ROOT)`, rows `wo-spkholda/b/c` (`requestor=user`), `session_id` bound after spawn, so the real `jarvis _hook` resolves them (`find_by_session`) and writes `hook:*` events there |
| Hook log | every hook command in the generated settings wrapped by `ROOT/.jarvis/scratch/hookwrap.sh`: appends the raw payload to `hooks.jsonl`, then pipes it to the worktree's `jarvis _hook`. PreToolUse matcher extended with `\|Artifact\|ArtifactComments` (as §3.3 will) |
| Env | script scrubs every `JARVIS_*`, `CLAUDE_CODE_*`, `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_JOB_DIR`; `JARVIS_HOME=/tmp/jarvis-spikehold-home`; settings env `JARVIS_WO_ID=wo-spkhold<role>`, `JARVIS_PROJECT_PATH=ROOT` |
| Monitor | `spike_artifact_hold.py monitor`: roster `[pid, status, state]` per session and `claude --version`, every 5 s, logged on change to `ROOT/.jarvis/scratch/monitor.log` |

| Role | Short | sid | Artifact(s) | For |
|---|---|---|---|---|
| a | `548669cc` | `548669cc-298c-42e0-af97-2899d2afb521` | https://claude.ai/artifact/UpLG8KNsUyPsCngNnXubJN | P1, P3, P7 subagent |
| b | `e207b6c8` | `e207b6c8-941a-4515-a3c3-ff1f515f816f` | watches https://claude.ai/artifact/JU754gmR89snAzsXVVZyim (PR 1683's) | P4 |
| c | `bf033d74` | `bf033d74-1ff4-462d-906d-78027fbb36f4` | C1 https://claude.ai/artifact/FmS1KHuCQQ98Kj4oHBGGGN, C2 https://claude.ai/artifact/6v9SefKhCoo8h8q9qCApPs | P2, P5, P7 |

## Side effect found while setting up: background worktree guard

Session a's first `Write` (to ROOT, a plain dir of the dev checkout) was refused by the
CLI itself:
`This background session hasn't isolated its changes yet. Call EnterWorktree first so edits land in a worktree instead of the shared checkout, ... (To disable this guard for this repo, set "worktree": {"bgIsolation": "none"} in .claude/settings.json.)`
It then ran `EnterWorktree {"name": "hold-probe-a"}` and relocated to
`/home/gonzalo/workspace/agentic_os/.claude/worktrees/hold-probe-a` (branch
`worktree-hold-probe-a`). Sessions b and c wrote nothing (c published files the script
pre-wrote).
