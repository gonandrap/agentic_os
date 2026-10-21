---
type: Jarvis Order Playbook
title: Release
description: Cutting or shipping a Jarvis release needs the release procedure and the gate protocol, not the architecture.
tags: [release, shipit]
jarvis_rank: 40
jarvis_match: [cut a release, cut the release, ship a release, ship the release, new release, shipit, deploy to production]
---
# Release

- Run the `shipit` skill; it is the whole procedure. Never cut a release by hand.
- Shipping is gated: `jarvis brief gates` before the first gated command.
- [Environments and releases](/engineering/environments.md) only if the deploy itself misbehaves.
- Do not read the codebase map or any spec unless a release step fails.
