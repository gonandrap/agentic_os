---
type: Jarvis Engineering Guide
title: Testing
description: Run only the targeted test files for what you changed; CI runs the full suite. LLM evals are opt-in and cost real calls.
resource: ../../../.serena/memories/testing.md
tags: [testing]
---
# Testing

- `uv sync --extra dev` once per worktree, then `uv run pytest tests/<file> -q -x`.
- Never the whole suite locally (>15 min; it outlives the prompt cache) — cite CI instead.
- LLM evals: `JARVIS_EVALS_LLM=1 uv run pytest evals/llm/<file> -q`.
- What covers what: Serena `read_memory testing`.
