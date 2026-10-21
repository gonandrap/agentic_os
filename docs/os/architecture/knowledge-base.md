---
type: Jarvis Architecture Concept
title: Knowledge base
description: The OS's durable memory — `jarvis learn` entries, pins, retraction, ranked search, and the bounded index a worker prompt carries.
tags: [architecture, knowledge]
---
# Knowledge base

- Verbs: `jarvis learn search|show|list|topics|add`; worker guide: `jarvis brief knowledge`.
- In prompts: `CentralStore.knowledge_brief` + `dispatch.render_knowledge_block` — pinned
  rules (capped), title-matched hints, a short index, a topic roll-call.
- Specs: `docs/superpowers/specs/2026-07-27-knowledge-on-demand-design.md`,
  `2026-08-24-ranked-knowledge-search.md`.
- This bundle is the STRUCTURAL map (architecture, playbooks); `jarvis learn` is the
  ledger of rulings and lessons. Concepts link to entries by id, never copy them.
