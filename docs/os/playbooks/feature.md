---
type: Jarvis Order Playbook
title: Feature child
description: One section of a planned feature. The materialised spec section is the source of truth; the rest of the spec is navigated, never read whole.
tags: [feature]
jarvis_rank: 20
jarvis_kinds: [planner, manager]
jarvis_child: true
---
# Feature child

1. Your spec section (path in this prompt), then `jarvis spec toc` / `jarvis spec section` for any section it cites.
2. What your dependencies shipped: the block in this prompt, then `jarvis wo show <id>`.
3. [Feature orders](/architecture/feature-orders.md) only if you touch planning, children or the manager.
4. [Codebase map](/architecture/codebase-map.md) for the modules your section names.
