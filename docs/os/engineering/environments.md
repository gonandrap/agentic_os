---
type: Jarvis Engineering Guide
title: Environments and releases
description: The dev checkout (a branch) versus production (a detached release tag), JARVIS_HOME, and the shipit release path.
resource: ../../../.serena/memories/dev-vs-prod-environments.md
tags: [environments, releases]
---
# Environments and releases

- `git symbolic-ref -q HEAD` succeeds in dev, fails in prod (detached at `jarvis-X.Y.Z`).
- Prod is read-only: fix in dev, release with the `shipit` skill.
- Detail: Serena `read_memory dev-vs-prod-environments`; `docs/DEPLOYMENT.md`.
