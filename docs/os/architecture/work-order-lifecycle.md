---
type: Jarvis Architecture Concept
title: Work-order lifecycle
description: The work-order state machine, headless `claude -p` turns, hooks, settlement from the latest turn, and self-healing retries.
resource: ../../../.serena/memories/work-order-lifecycle.md
tags: [architecture, work orders, turns]
---
# Work-order lifecycle

- Full text: Serena `read_memory work-order-lifecycle`.
- Transport: `src/jarvis/worker_session.py` module docstring, then
  `jarvis spec toc docs/superpowers/specs/2026-08-01-headless-turn-runtime-design.md`.
- What a worker starts with: [worker context](/architecture/worker-context.md).
- Message delivery: Serena `read_memory message-bus`.
