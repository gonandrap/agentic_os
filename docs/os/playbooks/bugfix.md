---
type: Jarvis Order Playbook
title: Bug fix
description: One defect in one area. Load the issue, the entries it cites, and the one module it names; widen only if the cause is elsewhere.
tags: [bugfix]
jarvis_rank: 30
jarvis_match: [fix, bug, broken, regression, crash, fails, wrong, issues/]
---
# Bug fix

1. The issue and every `kn-…` id it cites: `gh issue view`, `jarvis learn show <ids>`.
2. The named module only: Serena `get_symbols_overview`, then `find_symbol`.
3. [Testing](/engineering/testing.md) for the targeted test file to extend.
4. Only if the cause crosses modules: [Codebase map](/architecture/codebase-map.md), then the lifecycle concept it lands in.
