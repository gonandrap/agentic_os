---
type: Jarvis Architecture Concept
title: Worker context
description: What a worker's first request contains, who owns each part, and the 10K-token budget on the part Jarvis controls.
tags: [architecture, tokens, prompt cache]
timestamp: 2026-10-20T00:00:00Z
---
# Worker context

**Budget.** Jarvis's share of a worker's starting window must stay at or under
`context.START_BUDGET_TOKENS` (10,000, estimated as chars/4). Enforced by
`tests/test_worker_start_budget.py`; every dispatch records it as
`wo_turns.context_json.start_budget`. Jarvis's share is the brief, the appended system
prompt, the project CLAUDE.md, the SessionStart house style, and the Jarvis skill and agent
listings.

**Measured 2026-10-20 (Claude Code 2.1.304, wo-71b2ecad's first request):**

| owner | before | after |
|---|---|---|
| Jarvis (brief, CLAUDE.md, append, hooks, listings) | 82.0K chars (~20.5K est) | 35.6K chars (~8.9K est) |
| user config (plugins, synced skills, global CLAUDE.md) | 19.9K chars | 21.1K chars |
| Claude Code (system prompt, tools, env) | 54.3K chars | 36.4K chars |
| real first-request tokens | 51,408 | 34,707 (Haiku probe) |

The real tokenizer counts ~1.3x chars/4 on this content (tool JSON is dense), so Jarvis's
8.9K estimate is ~13K real tokens.

**Levers, all applied at dispatch:**
- `worker.denied_tools` (default `Workflow`, `ScheduleWakeup`, `ReportFindings`,
  `ListAgents`, `Agent(statusline-setup)`) goes into `permissions.deny`. A bare-name deny
  removes the schema from the request (~14K chars); verified on 2.1.304.
- `wiring.bundled_skills` now defaults to off (`disableBundledSkills`, ~6.6K chars).
- `wiring.chrome: false` launches workers with `--no-chrome` (~1.6K tokens per call). Chrome
  is enabled from the user's global config, which no settings key reaches (issue 1370).
- Pinned knowledge renders at most `os.knowledge_pinned_chars` (360) per entry plus its id;
  the index is `knowledge_digest_limit` 8 / `_chars` 1000; the topic roll-call names the 12
  largest topics.
- Read-only kinds (investigator, analyst) get a one-line gate pointer, not the protocol.
- One [playbook](/index.md) per order replaces the "read everything" posture.
- CLAUDE.md is one line per command; the detail moved to `/operator/*.md`.

**Outside Jarvis's levers:** Claude Code's base prompt and core tool schemas (~36K chars).
claude.ai synced skills (`anthropic-skills:*`, ~10.5K chars) were too on 2.1.304; on 2.1.316
`skillOverrides` drops one by its qualified name, so `jarvis config wiring` lists each one
(read from `~/.claude/skills/synced/*/manifest.json`) and `wiring.disabled_skills` unwires it. The user's
own plugins (superpowers: ~3.4K-char hook + ~2.9K listing) stay wired by the opt-out
ruling; deselect them per project on /config to save ~1.6K tokens per start.
