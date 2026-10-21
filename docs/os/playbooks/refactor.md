---
type: Jarvis Order Playbook
title: Refactor
description: A structural change across modules. This is the one kind that should load the architecture before touching code.
tags: [refactor]
jarvis_rank: 25
jarvis_match: [refactor, restructure, rename, split, extract, move, consolidate, deduplicate, layering, architecture]
---
# Refactor

1. [Codebase map](/architecture/codebase-map.md) — layering and import rules first.
2. The lifecycle concept the change lives in: [work orders](/architecture/work-order-lifecycle.md), [feature orders](/architecture/feature-orders.md) or [gates](/architecture/gates.md).
3. Serena `find_referencing_symbols` on every symbol you move or rename.
4. [Testing](/engineering/testing.md) — which test files cover the modules you touch.
